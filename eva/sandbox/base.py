"""沙箱执行器的公共部分：会话工作目录、输入暂存、产物校验、空闲回收、并发上限。

后端只负责 _execute：在给定的工作目录里把 .eva/run.py 跑起来，返回 (退出码, stdout, stderr)。
用户代码自己的失败（退出码非 0）是正常结果，原样交给模型；沙箱本身的故障抛 E8xxx。
"""
import logging
import os
import re
import shutil
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

from eva.common import clock, logs
from eva.contracts.errors import Code, EvaError
from eva.sandbox.config import SandboxConfig

log = logging.getLogger(__name__)
META_DIR = ".eva"  # 运行脚本和最近使用时间；不是产物，不进清单
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MAX_REJECTED_LISTED = 20


@dataclass(frozen=True)
class Artifact:
    path: str  # 相对工作目录，正斜杠
    bytes: int


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    files: Tuple[Artifact, ...]  # 本次新增或修改的合规文件
    rejected: Tuple[dict, ...] = ()  # 本次新增或修改但不合规的文件（类型、大小、非普通文件），没有登记为产物，原因在每项里


def _safe_relative(rel: str) -> Path:
    parts = rel.split("/")
    if not rel or any(p in ("", ".", "..") or "\\" in p or ":" in p for p in parts) or parts[0] == META_DIR:
        raise EvaError(Code.INVALID_VALUE, "文件的相对路径不合法", path=rel)
    return Path(*parts)


class Sandbox:
    enforcement = "partial"  # 后端如实报告：full 或 partial

    def __init__(self, cfg: SandboxConfig):
        self._cfg = cfg
        self._slots = threading.Semaphore(cfg.max_concurrency)
        self._reap_lock, self._last_reap = threading.Lock(), 0.0

    def _execute(self, workdir: Path, cancel: threading.Event) -> Tuple[int, bytes, bytes]:
        raise NotImplementedError

    def run(self, tenant_id: str, session_id: str, code: str, files: Mapping[str, bytes],
            cancel: threading.Event) -> ExecResult:
        """files 是要放进工作目录的输入（相对路径 → 字节）。同一会话的调用由上层串行，这里不加会话锁。"""
        workdir = self._workdir(tenant_id, session_id)
        self._reap_if_due()
        while not self._slots.acquire(timeout=0.1):
            if cancel.is_set():
                raise EvaError(Code.TOOL_ABORTED, "取消，沙箱运行未开始")
        try:
            self._stage(workdir, code, files)
            before = self._scan(workdir)
            started = time.monotonic()
            exit_code, out, err = self._execute(workdir, cancel)
            duration = time.monotonic() - started
        finally:
            self._slots.release()
        artifacts, rejected = self._collect(workdir, before)
        logs.emit(log, logging.INFO, "沙箱运行完成", backend=type(self).__name__, exit_code=exit_code,
                  seconds=round(duration, 1), stdout_bytes=len(out), stderr_bytes=len(err), artifacts=len(artifacts),
                  rejected=len(rejected))
        return ExecResult(exit_code, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace"),
                          duration, artifacts, rejected)

    def _workdir(self, tenant_id: str, session_id: str) -> Path:
        for name in (tenant_id, session_id):
            if not _ID.fullmatch(name):
                raise EvaError(Code.INVALID_VALUE, "租户或会话 ID 不能用作目录名", value=name)
        return self._cfg.work_root / tenant_id / session_id

    def reap(self) -> int:
        """删除空闲超过 idle_seconds 的会话目录，返回删了几个。"""
        root, removed, cutoff = self._cfg.work_root, 0, clock.unix() - self._cfg.idle_seconds
        if not root.is_dir():
            return 0
        for session_dir in [p for t in root.iterdir() if t.is_dir() for p in t.iterdir() if p.is_dir()]:
            marker = session_dir / META_DIR / "last_used"
            try:
                last = (marker if marker.exists() else session_dir).stat().st_mtime
                if last < cutoff:
                    shutil.rmtree(session_dir)
                    removed += 1
                    logs.emit(log, logging.INFO, "回收过期会话目录", session=session_dir.name)
            except OSError as e:
                raise EvaError(Code.SANDBOX_UNAVAILABLE, "回收过期的会话目录失败", path=str(session_dir),
                               error=str(e)) from e
        return removed

    def discard(self, tenant_id: str, session_id: str) -> None:
        """删掉整个会话目录。一次性任务（如文件解析）用完即清，不等空闲回收。目录本来就不在算已清。"""
        try:
            shutil.rmtree(self._workdir(tenant_id, session_id))
        except FileNotFoundError:
            return
        except OSError as e:
            raise EvaError(Code.SANDBOX_UNAVAILABLE, "删除会话工作目录失败", session_id=session_id, error=str(e)) from e

    def _reap_if_due(self) -> None:
        with self._reap_lock:
            now = clock.unix()
            if now - self._last_reap < self._cfg.reap_interval_seconds:
                return
            self._last_reap = now
        self.reap()

    def _stage(self, workdir: Path, code: str, files: Mapping[str, bytes]) -> None:
        try:
            meta = workdir / META_DIR
            meta.mkdir(parents=True, exist_ok=True)
            (meta / "run.py").write_text(code, encoding="utf-8")
            (meta / "last_used").write_text(str(clock.unix()), encoding="utf-8")
            for rel, data in files.items():
                target = workdir / _safe_relative(rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        except OSError as e:
            raise EvaError(Code.SANDBOX_UNAVAILABLE, "写入会话工作目录失败", path=str(workdir), error=str(e)) from e

    def _scan(self, workdir: Path) -> Dict[str, Tuple[int, int, bool]]:
        """相对路径 → (字节数, 修改时间 ns, 是否普通文件)。符号链接和设备按"非普通文件"登记，不跟随。"""
        found = {}
        for dirpath, dirnames, filenames in os.walk(workdir):
            if Path(dirpath) == workdir and META_DIR in dirnames:
                dirnames.remove(META_DIR)
            for name in filenames:
                full = Path(dirpath) / name
                st = full.lstat()
                found[full.relative_to(workdir).as_posix()] = (st.st_size, st.st_mtime_ns, stat.S_ISREG(st.st_mode))
        return found

    def _collect(self, workdir: Path, before: Dict[str, Tuple[int, int, bool]]) -> Tuple[Tuple[Artifact, ...], Tuple[dict, ...]]:
        """校验并列出本次新增或修改的文件。总量、个数超限是错误；个别文件不合规（类型、大小、非普通文件）只拒收那几个，
        合规的照常登记，不合规的原因随结果返回。第三方库在工作目录里留下的缓存之类的副产物不应拖垮整次运行。"""
        now = self._scan(workdir)
        total = sum(size for size, _, _ in now.values())
        if total > self._cfg.max_workdir_bytes:
            raise EvaError(Code.SANDBOX_OUTPUT_LIMIT, "会话工作目录总量超限，请先删除不需要的文件", reason="workdir",
                           bytes=total, max_bytes=self._cfg.max_workdir_bytes)
        changed = sorted(p for p, v in now.items() if before.get(p) != v)
        if len(changed) > self._cfg.max_files:
            raise EvaError(Code.SANDBOX_OUTPUT_LIMIT, "本次产生的文件个数超限", reason="files",
                           count=len(changed), max_files=self._cfg.max_files)
        violations = {p: self._violation(p, now[p][0], now[p][2]) for p in changed}
        rejected = tuple(v for v in violations.values() if v)
        if rejected:
            logs.emit(log, logging.WARNING, "沙箱产物里有不合规的文件，已拒收", count=len(rejected), sample=list(rejected[:5]))
        return tuple(Artifact(p, now[p][0]) for p in changed if not violations[p]), rejected[:_MAX_REJECTED_LISTED]

    def _violation(self, path: str, size: int, regular: bool) -> Optional[dict]:
        ext = os.path.splitext(path)[1].lower()
        if not regular:
            return {"path": path, "reason": "not_regular_file"}
        if ext not in self._cfg.allowed_extensions:
            return {"path": path, "reason": "extension", "extension": ext}
        if size > self._cfg.max_file_bytes:
            return {"path": path, "reason": "too_large", "bytes": size, "max_bytes": self._cfg.max_file_bytes}
        return None

    def read_artifact(self, tenant_id: str, session_id: str, path: str) -> bytes:
        """把会话工作目录里的一个文件取给宿主。路径的每一级都不能是符号链接：
        容器里建一个指向 /etc 的链接，宿主跟随它就会读到宿主文件。"""
        current = self._workdir(tenant_id, session_id)
        rel = _safe_relative(path)
        try:
            for part in rel.parts:
                current = current / part
                st = current.lstat()
                if stat.S_ISLNK(st.st_mode):
                    raise EvaError(Code.SANDBOX_ARTIFACT_REJECTED, "路径中含符号链接",
                                   rejected=[{"path": path, "reason": "not_regular_file"}])
            violation = self._violation(path, st.st_size, stat.S_ISREG(st.st_mode))
            if violation:
                raise EvaError(Code.SANDBOX_ARTIFACT_REJECTED, "文件不合规，不能保存", rejected=[violation],
                               allowed_extensions=list(self._cfg.allowed_extensions))
            return current.read_bytes()
        except (FileNotFoundError, NotADirectoryError) as e:
            raise EvaError(Code.FILE_NOT_FOUND, "工作目录里没有这个文件", path=path) from e
        except OSError as e:
            raise EvaError(Code.SANDBOX_UNAVAILABLE, "读取会话工作目录失败", path=path, error=str(e)) from e
