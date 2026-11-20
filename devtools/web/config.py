from dataclasses import dataclass
from pathlib import Path

import yaml

from eva.contracts.errors import Code, EvaError

PATH = Path(__file__).resolve().parent / "config.yaml"


@dataclass(frozen=True)
class WebConfig:
    host: str
    port: int
    session_ttl_seconds: int
    login_max_failures: int
    login_lock_seconds: int
    max_upload_mb: int
    results_limit: int
    sessions_limit: int
    tenants_limit: int
    answer_timeout_seconds: int


def load(path: Path = PATH) -> WebConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了EVA测评系统配置", path=str(path), error=str(e)) from e
    fields = WebConfig.__dataclass_fields__
    if not isinstance(raw, dict) or set(raw) != set(fields):
        raise EvaError(Code.CONFIG_INVALID, f"EVA测评系统配置必须正好有这些项：{', '.join(fields)}", path=str(path))
    if not isinstance(raw["host"], str) or not raw["host"].strip():
        raise EvaError(Code.CONFIG_INVALID, "host 必须是非空字符串", value=repr(raw["host"]))
    for key in fields:
        v = raw[key]
        if key != "host" and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
            raise EvaError(Code.CONFIG_INVALID, f"{key} 必须是正整数", value=repr(v))
    return WebConfig(**raw)
