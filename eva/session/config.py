"""configs/session.yaml 的加载与校验。"""
from dataclasses import dataclass
from pathlib import Path

import yaml

from eva.contracts.errors import Code, EvaError

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "configs" / "session.yaml"


@dataclass(frozen=True)
class SessionConfig:
    stale_hold_seconds: float
    sweep_interval_seconds: float
    sweep_batch: int


def load(path: Path = DEFAULT_PATH) -> SessionConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了会话配置", path=str(path), error=str(e)) from e
    if not isinstance(raw, dict):
        raise EvaError(Code.CONFIG_INVALID, "会话配置顶层必须是映射", path=str(path))
    values = {}
    for key, kind in (("stale_hold_seconds", float), ("sweep_interval_seconds", float), ("sweep_batch", int)):
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise EvaError(Code.CONFIG_INVALID, f"{key} 必须是正数", value=repr(v))
        values[key] = kind(v)
    return SessionConfig(**values)
