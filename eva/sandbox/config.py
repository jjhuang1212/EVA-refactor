"""configs/sandbox.yaml 的加载与校验。缺项和非法值直接报 E0003，不补默认值。"""
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import yaml

from eva.contracts.errors import Code, EvaError

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "configs" / "sandbox.yaml"
BACKENDS = ("local", "docker")
ENFORCEMENTS = ("partial", "full")  # 顺序即强弱


@dataclass(frozen=True)
class DockerConfig:
    bin: str
    image: str
    runtime: str  # 空串 = docker 默认
    memory_mb: int
    cpus: float
    pids: int
    tmpfs_mb: int


@dataclass(frozen=True)
class SandboxConfig:
    backend: str
    require_enforcement: str
    work_root: Path
    idle_seconds: float
    reap_interval_seconds: float
    max_concurrency: int
    timeout_seconds: float
    max_output_bytes: int
    max_input_bytes: int
    max_files: int
    max_file_bytes: int
    allowed_extensions: Tuple[str, ...]
    max_workdir_bytes: int
    docker: DockerConfig


def _positive(section: dict, key: str, kind=int, prefix: str = ""):
    v = section.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0 or (kind is int and v != int(v)):
        raise EvaError(Code.CONFIG_INVALID, f"沙箱配置 {prefix}{key} 必须是正数" + ("（整数）" if kind is int else ""),
                       value=repr(v))
    return kind(v)


def _text(section: dict, key: str, allowed=None, empty_ok: bool = False) -> str:
    v = section.get(key)
    if not isinstance(v, str) or (not v and not empty_ok) or (allowed is not None and v not in allowed):
        raise EvaError(Code.CONFIG_INVALID, f"沙箱配置 {key} 缺失或取值非法", value=repr(v),
                       allowed=list(allowed) if allowed else None)
    return v


def load(path: Path = DEFAULT_PATH) -> SandboxConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了沙箱配置", path=str(path), error=str(e)) from e
    if not isinstance(raw, dict) or not isinstance(raw.get("docker"), dict):
        raise EvaError(Code.CONFIG_INVALID, "沙箱配置顶层必须是映射且含 docker 小节", path=str(path))
    ext = raw.get("allowed_extensions")
    if not isinstance(ext, list) or not ext or not all(
            isinstance(x, str) and x.startswith(".") and x == x.lower() and len(x) > 1 for x in ext):
        raise EvaError(Code.CONFIG_INVALID, "allowed_extensions 必须是小写、带点的扩展名列表", value=repr(ext))
    work_root = Path(_text(raw, "work_root"))
    d = raw["docker"]
    return SandboxConfig(
        backend=_text(raw, "backend", BACKENDS),
        require_enforcement=_text(raw, "require_enforcement", ENFORCEMENTS),
        work_root=work_root if work_root.is_absolute() else ROOT / work_root,
        idle_seconds=_positive(raw, "idle_seconds", float),
        reap_interval_seconds=_positive(raw, "reap_interval_seconds", float),
        max_concurrency=_positive(raw, "max_concurrency"),
        timeout_seconds=_positive(raw, "timeout_seconds", float),
        max_output_bytes=_positive(raw, "max_output_bytes"),
        max_input_bytes=_positive(raw, "max_input_bytes"),
        max_files=_positive(raw, "max_files"),
        max_file_bytes=_positive(raw, "max_file_bytes"),
        allowed_extensions=tuple(ext),
        max_workdir_bytes=_positive(raw, "max_workdir_bytes"),
        docker=DockerConfig(
            bin=_text(d, "bin"), image=_text(d, "image"), runtime=_text(d, "runtime", empty_ok=True),
            memory_mb=_positive(d, "memory_mb", prefix="docker."), cpus=_positive(d, "cpus", float, "docker."),
            pids=_positive(d, "pids", prefix="docker."), tmpfs_mb=_positive(d, "tmpfs_mb", prefix="docker.")),
    )
