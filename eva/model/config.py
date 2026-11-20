"""configs/model.yaml 的加载与校验。缺项在加载时就抛 E5006，不带着缺项跑到第一次真实调用。"""
import os
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional, Tuple

import yaml
from dotenv import load_dotenv

from eva.contracts.errors import Code, EvaError

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "configs" / "model.yaml"
_PRICE_KEYS = ("input_cache_miss", "input_cache_hit", "output")


@dataclass(frozen=True)
class PriceTier:
    up_to_input_tokens: Optional[int]  # 本次请求输入 token 总数的档上限（含）；None 是最后一档，无上限
    prices: Dict[str, Decimal]  # 元/百万 token


@dataclass(frozen=True)
class PeakRule:
    multiplier: Decimal
    isoweekdays: Tuple[int, ...]
    windows: Tuple[Tuple[time, time], ...]  # [起, 止)

    def active(self, at: datetime) -> bool:
        return at.isoweekday() in self.isoweekdays and any(s <= at.time() < e for s, e in self.windows)


@dataclass(frozen=True)
class ModelSpec:
    name: str
    context_window: int
    api_key_env: str
    base_url_env: str
    extra_body: dict
    thinking_extra_body: dict  # 调用要求思考时叠在 extra_body 上；空表示这个模型没有可开关的思考
    price_tiers: Tuple[PriceTier, ...]
    peak: Optional[PeakRule]
    cache_hit_path: str
    quota_statuses: Tuple[int, ...]
    quota_code_prefixes: Tuple[str, ...]
    overflow_codes: Tuple[str, ...]
    overflow_message_markers: Tuple[str, ...]
    input: Tuple[str, ...]  # 能收的输入类型，text 必有；声明了 image 的模型才能当 vision

    def prices_per_million(self, input_tokens: int, at: datetime) -> Dict[str, Decimal]:
        """元/百万 token。档由本次请求的输入 token 总数定，整条请求按该档计价；高峰时段乘倍数。"""
        tier = next(t for t in self.price_tiers
                    if t.up_to_input_tokens is None or input_tokens <= t.up_to_input_tokens)
        if self.peak and self.peak.active(at):
            return {k: v * self.peak.multiplier for k, v in tier.prices.items()}
        return tier.prices


@dataclass(frozen=True)
class ModelConfig:
    primary: str
    fallback: str
    vision: str  # 看图专用，不参与主备切换，必须声明 image
    image_token_bound: int  # 预扣时一张图按多少 token 算（上界），不按 base64 字符数
    pricing_version: str
    models: Dict[str, ModelSpec]
    max_retries: int
    backoff_base_seconds: float
    backoff_jitter_seconds: float
    max_delay_seconds: float
    attempt_seconds: float
    max_in_flight: int
    queue_timeout_seconds: float
    breaker_failures: int
    breaker_probe_seconds: float
    max_output_tokens: int
    budget_per_day_cny: Decimal
    reasoning_max_chars: int  # 记录模型思考内容的字数上限，0 表示不记录
    call_record: bool  # 每次模型尝试是否写调用记录，见 client.py 的 recorder


def _bad(msg: str, **details) -> EvaError:
    return EvaError(Code.MODEL_CONFIG_INVALID, msg, **details)


def _number(raw: dict, key: str, kind=float, minimum=0):
    v = raw.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < minimum or (kind is int and v != int(v)):
        raise _bad(f"{key} 必须是不小于 {minimum} 的{'整数' if kind is int else '数'}", value=repr(v))
    return kind(v)


def _flag(raw: dict, key: str) -> bool:
    v = raw.get(key)
    if not isinstance(v, bool):
        raise _bad(f"{key} 必须是 true 或 false", value=repr(v))
    return v


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _tiers(name: str, raw: dict) -> Tuple[PriceTier, ...]:
    rows = raw.get("price_tiers")
    if not isinstance(rows, list) or not rows:
        raise _bad("缺 price_tiers", model=name)
    tiers, floor = [], 0
    for i, row in enumerate(rows):
        row = row if isinstance(row, dict) else {}
        missing = [k for k in _PRICE_KEYS if k not in row]
        if missing:
            raise _bad("缺单价", model=name, tier=i, missing=missing)
        if not all(_is_number(row[k]) and row[k] >= 0 for k in _PRICE_KEYS):
            raise _bad("单价必须是非负数", model=name, tier=i)
        limit = row.get("up_to_input_tokens")
        if i == len(rows) - 1:
            if limit is not None:
                raise _bad("最后一档不设 up_to_input_tokens", model=name, tier=i)
        elif isinstance(limit, bool) or not isinstance(limit, int) or limit <= floor:
            raise _bad("非最后一档的 up_to_input_tokens 必须是递增的正整数", model=name, tier=i, value=repr(limit))
        else:
            floor = limit
        tiers.append(PriceTier(limit, {k: Decimal(str(row[k])) for k in _PRICE_KEYS}))
    return tuple(tiers)


def _peak(name: str, raw: dict) -> Optional[PeakRule]:
    p = raw.get("peak")
    if p is None:
        return None
    p = p if isinstance(p, dict) else {}
    mult, days, wins = p.get("multiplier"), p.get("isoweekdays"), p.get("windows")
    if not _is_number(mult) or mult <= 0:
        raise _bad("peak.multiplier 必须是正数", model=name, value=repr(mult))
    if (not isinstance(days, list) or not days
            or any(isinstance(d, bool) or not isinstance(d, int) or not 1 <= d <= 7 for d in days)):
        raise _bad("peak.isoweekdays 必须是 1-7 的整数列表", model=name, value=repr(days))
    if not isinstance(wins, list) or not wins:
        raise _bad("peak.windows 必须是 HH:MM-HH:MM 的列表", model=name, value=repr(wins))
    windows = []
    for w in wins:
        try:
            start, end = (time.fromisoformat(x) for x in str(w).split("-"))
        except ValueError:
            raise _bad("peak.windows 项必须是 HH:MM-HH:MM", model=name, value=repr(w)) from None
        if start >= end:
            raise _bad("peak.windows 项的起点必须早于止点", model=name, value=repr(w))
        windows.append((start, end))
    return PeakRule(Decimal(str(mult)), tuple(days), tuple(windows))


def _input(name: str, raw) -> Tuple[str, ...]:
    if not isinstance(raw, list) or "text" not in raw or not set(raw) <= {"text", "image"}:
        raise _bad("input 必须是列表，含 text，其余只能是 image", model=name, value=repr(raw))
    return tuple(dict.fromkeys(raw))


def _spec(name: str, raw: dict) -> ModelSpec:
    for k in ("api_key_env", "base_url_env", "cache_hit_path"):
        if not raw.get(k):
            raise _bad(f"缺 {k}", model=name)
    window = raw.get("context_window")
    if isinstance(window, bool) or not isinstance(window, int) or window < 1:
        raise _bad("context_window 必须是正整数", model=name, value=repr(window))
    return ModelSpec(
        name=name,
        context_window=window,
        api_key_env=raw["api_key_env"],
        base_url_env=raw["base_url_env"],
        extra_body=dict(raw.get("extra_body") or {}),
        thinking_extra_body=dict(raw.get("thinking_extra_body") or {}),
        price_tiers=_tiers(name, raw),
        peak=_peak(name, raw),
        cache_hit_path=raw["cache_hit_path"],
        quota_statuses=tuple(raw.get("quota_statuses") or ()),
        quota_code_prefixes=tuple(raw.get("quota_code_prefixes") or ()),
        overflow_codes=tuple(raw.get("overflow_codes") or ()),
        overflow_message_markers=tuple(raw.get("overflow_message_markers") or ()),
        input=_input(name, raw.get("input")),
    )


def load(path: Path = DEFAULT_PATH) -> ModelConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise _bad("读不到或解析不了模型配置", path=str(path), error=str(e)) from e
    if not isinstance(raw, dict):
        raise _bad("模型配置顶层必须是映射", path=str(path))
    models = {n: _spec(n, r or {}) for n, r in (raw.get("models") or {}).items()}
    for role in ("primary", "fallback"):
        if raw.get(role) not in models:
            raise _bad(f"{role} 不在 models 里", value=raw.get(role))
    if raw.get("vision") not in models:
        raise _bad("vision 不在 models 里", value=raw.get("vision"))
    if "image" not in models[raw["vision"]].input:
        raise _bad("vision 指向的模型没有声明 input: image", model=raw["vision"])
    if not raw.get("pricing_version"):
        raise _bad("缺 pricing_version")
    retry, breaker = raw.get("retry") or {}, raw.get("breaker") or {}
    return ModelConfig(
        primary=raw["primary"],
        fallback=raw["fallback"],
        vision=raw["vision"],
        image_token_bound=_number(raw, "image_token_bound", int, minimum=1),
        pricing_version=str(raw["pricing_version"]),
        models=models,
        max_retries=_number(retry, "max_retries", int),
        backoff_base_seconds=_number(retry, "backoff_base_seconds"),
        backoff_jitter_seconds=_number(retry, "backoff_jitter_seconds"),
        max_delay_seconds=_number(retry, "max_delay_seconds"),
        attempt_seconds=_number(raw.get("timeout") or {}, "attempt_seconds", minimum=1),
        max_in_flight=_number(raw.get("concurrency") or {}, "max_in_flight", int, minimum=1),
        queue_timeout_seconds=_number(raw.get("concurrency") or {}, "queue_timeout_seconds", minimum=1),
        breaker_failures=_number(breaker, "open_after_consecutive_failures", int, minimum=1),
        breaker_probe_seconds=_number(breaker, "probe_after_seconds"),
        max_output_tokens=_number(raw, "max_output_tokens", int, minimum=1),
        budget_per_day_cny=Decimal(str(_number(raw, "budget_per_day_cny"))),
        reasoning_max_chars=_number(raw, "reasoning_max_chars", int),
        call_record=_flag(raw, "call_record"),
    )


def credentials(spec: ModelSpec) -> Tuple[str, str]:
    """读环境变量里的密钥和地址。缺了抛 E5006，不当成空串发出去再以 401 回来。"""
    load_dotenv()
    missing = [v for v in (spec.api_key_env, spec.base_url_env) if not os.environ.get(v)]
    if missing:
        raise _bad("缺少环境变量", model=spec.name, missing=missing)
    return os.environ[spec.api_key_env], os.environ[spec.base_url_env]

