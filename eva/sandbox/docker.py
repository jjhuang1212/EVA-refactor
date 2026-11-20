"""服务器后端：每次调用一个一次性容器，会话工作目录挂进 /work。

容器断网、根文件系统只读、去掉全部 capability，以宿主进程的 uid 运行（工作目录里的文件归应用账号），
内存、CPU、进程数受限；运行时可选 gVisor（runsc）。容器里的进程跑完即销毁，会话内保留的只有工作目录里的文件。
"""
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from eva.common import ids
from eva.contracts.errors import Code, EvaError
from eva.sandbox.base import META_DIR, Sandbox
from eva.sandbox.config import SandboxConfig
from eva.sandbox.process import run_capped, tail

_ENV_KEEP = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")  # 只给 docker 客户端自己用
# docker 客户端自己出错（而不是容器里的代码退出）时的退出码和 stderr 特征，两者同时满足才算沙箱故障
_RUNNER_EXIT = (125, 126, 127)
_RUNNER_SIGNS = ("docker:", "error response from daemon", "cannot connect to the docker daemon", "unable to find image")
_OOM_EXIT = 137  # 128 + SIGKILL，容器被内存限额杀掉


class DockerSandbox(Sandbox):
    enforcement = "full"

    def __init__(self, cfg: SandboxConfig, run: Callable = run_capped, user: Optional[str] = None):
        super().__init__(cfg)
        if user is None:
            if not hasattr(os, "getuid"):
                raise EvaError(Code.CONFIG_INVALID, "docker 沙箱后端只能在 Linux 上运行；开发机请用 local")
            user = f"{os.getuid()}:{os.getgid()}"
        self._run, self._user = run, user

    def _command(self, name: str, workdir: Path) -> List[str]:
        d = self._cfg.docker
        memory = f"{d.memory_mb}m"
        return [
            d.bin, "run", "--rm", "--name", name, *(["--runtime", d.runtime] if d.runtime else []),
            "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--memory", memory, "--memory-swap", memory, "--cpus", str(d.cpus), "--pids-limit", str(d.pids),
            "--user", self._user, "--tmpfs", f"/tmp:rw,noexec,nosuid,size={d.tmpfs_mb}m",
            "-v", f"{workdir.resolve().as_posix()}:/work", "-w", "/work",
            "-e", "PYTHONPATH=/work", "-e", "PYTHONUTF8=1", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "HOME=/tmp", "-e", "MPLCONFIGDIR=/tmp",
            d.image, "python", "-u", "-B", f"{META_DIR}/run.py",
        ]

    def _execute(self, workdir: Path, cancel: threading.Event) -> Tuple[int, bytes, bytes]:
        name, bin_ = f"eva-sbx-{ids.new_id().lower()}", self._cfg.docker.bin
        env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}

        def remove() -> None:
            # 杀掉 docker 客户端不会停容器，必须按名字删；容器已退出时 rm 返回非 0，不是故障
            subprocess.run([bin_, "rm", "-f", name], capture_output=True, env=env, timeout=30)

        code, out, err = self._run(self._command(name, workdir), workdir, env, self._cfg.timeout_seconds,
                                   self._cfg.max_output_bytes, cancel, on_kill=remove)
        if code in _RUNNER_EXIT and any(s in err.decode("utf-8", errors="replace").lower() for s in _RUNNER_SIGNS):
            raise EvaError(Code.SANDBOX_UNAVAILABLE, "docker 没能启动沙箱容器，代码没有运行", exit_code=code,
                           stderr_tail=tail(err))
        if code == _OOM_EXIT:
            raise EvaError(Code.SANDBOX_MEMORY_EXCEEDED, "代码占用内存超过限额，已被终止",
                           memory_mb=self._cfg.docker.memory_mb, stdout_tail=tail(out), stderr_tail=tail(err))
        return code, out, err
