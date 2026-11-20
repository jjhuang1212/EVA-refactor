"""configs/wecom.yaml 的加载与校验。企微的所有数字都在这里，核心层不出现。"""
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import yaml

from eva.contracts.errors import Code, EvaError

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "configs" / "wecom.yaml"

_NUMBERS = {  # 键 -> 类型；全部要求为正
    "http_timeout_seconds": float, "media_timeout_seconds": float, "token_margin_seconds": float,
    "text_max_bytes": int, "max_text_parts": int, "window_hours": float, "quota_per_window": int, "reserve_for_final": int, "sync_limit": int,
    "reserve_count": int, "block_ms": int, "claim_idle_ms": int, "max_parallel_tenants": int, "max_queued_messages": int, "listen_port": int, "file_limit_mb": int, "image_limit_mb": int, "voice_limit_mb": int,
    "busy_notice_seconds": float, "reconcile_seconds": float,
    "menu_max_items": int, "menu_head_max_bytes": int, "menu_item_max_bytes": int,
}
_STRINGS = ("api_base", "key_prefix", "listen_host", "reset_command")


@dataclass(frozen=True)
class WecomConfig:
    api_base: str
    http_timeout_seconds: float
    media_timeout_seconds: float
    token_margin_seconds: float
    text_max_bytes: int
    max_text_parts: int
    window_hours: float
    quota_per_window: int
    reserve_for_final: int
    sync_limit: int
    reserve_count: int
    block_ms: int
    claim_idle_ms: int
    max_parallel_tenants: int
    max_queued_messages: int
    key_prefix: str
    listen_host: str
    listen_port: int
    reset_command: str
    file_limit_mb: int
    image_limit_mb: int
    voice_limit_mb: int
    busy_notice_seconds: float
    reconcile_seconds: float
    menu_max_items: int
    menu_head_max_bytes: int
    menu_item_max_bytes: int
    file_extensions: Tuple[str, ...]  # 小写，不带点


def load(path: Path = DEFAULT_PATH) -> WecomConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了企微配置", path=str(path), error=str(e)) from e
    if not isinstance(raw, dict):
        raise EvaError(Code.CONFIG_INVALID, "企微配置顶层必须是映射", path=str(path))
    values = {}
    for key, kind in _NUMBERS.items():
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0 or (kind is int and v != int(v)):
            raise EvaError(Code.CONFIG_INVALID, f"{key} 必须是正{'整数' if kind is int else '数'}", value=repr(v))
        values[key] = kind(v)
    for key in _STRINGS:
        v = raw.get(key)
        if not isinstance(v, str) or not v.strip():
            raise EvaError(Code.CONFIG_INVALID, f"{key} 必须是非空字符串", value=repr(v))
        values[key] = v.strip()
    exts = raw.get("file_extensions")
    if not isinstance(exts, list) or not exts or not all(isinstance(e, str) and e.strip() and "." not in e for e in exts):
        raise EvaError(Code.CONFIG_INVALID, "file_extensions 必须是非空的扩展名列表（不带点）", value=repr(exts))
    values["file_extensions"] = tuple(e.strip().lower() for e in exts)
    if values["text_max_bytes"] < 4:  # 一个 UTF-8 字符最多 4 字节，更小就切不出段
        raise EvaError(Code.CONFIG_INVALID, "text_max_bytes 不能小于 4", value=values["text_max_bytes"])
    if values["reserve_for_final"] >= values["quota_per_window"]:
        raise EvaError(Code.CONFIG_INVALID, "reserve_for_final 必须小于 quota_per_window", value=values["reserve_for_final"])
    if values["sync_limit"] > 1000:
        raise EvaError(Code.CONFIG_INVALID, "sync_limit 不能超过企微上限 1000", value=values["sync_limit"])
    return WecomConfig(**values)
