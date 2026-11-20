"""configs/loop.yaml 的加载与校验。"""
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Tuple

import yaml

from eva.contracts.errors import Code, EvaError

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "configs" / "loop.yaml"


SPILL_NOTICE_RESERVE = 500  # 溢出说明文字（含对象键）的字节上界


@dataclass(frozen=True)
class LoopConfig:
    max_steps: int
    gate_max_retries: int
    max_parallel_tools: int
    compact_trigger_tokens: int
    compact_keep_tokens: int
    compact_max_failures: int
    trim_trigger_tokens: int
    trim_min_freed_tokens: int
    trim_keep_recent: int
    trim_tools: Tuple[str, ...]  # 旧结果可以被清掉的工具：结果大、需要时能重新取
    restore_skills_total_bytes: int
    restore_files_max: int
    spill_threshold_bytes: int
    spill_preview_bytes: int
    think_first_steps: int
    think_after_tools: Tuple[str, ...]  # 上一步调用了其中任一个工具的步骤要求思考
    wrap_up_steps: int = 0  # 步数到上限后，只开放 wrap_up_tools 再跑这么多步，用来交付已有的结果；0 或名单为空表示不收尾
    wrap_up_tools: Tuple[str, ...] = ()
    empty_max_retries: int = 0  # 模型既没调工具也没给文字（厂商偶发的空返回）时，同一步原样重调几次


def load(path: Path = DEFAULT_PATH) -> LoopConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了循环配置", path=str(path), error=str(e)) from e
    if not isinstance(raw, dict):
        raise EvaError(Code.CONFIG_INVALID, "循环配置顶层必须是映射", path=str(path))
    values = {}
    for key, minimum in (("max_steps", 1), ("gate_max_retries", 0), ("max_parallel_tools", 1),
                         ("compact_trigger_tokens", 1), ("compact_keep_tokens", 1), ("compact_max_failures", 1),
                         ("trim_trigger_tokens", 1), ("trim_min_freed_tokens", 1), ("trim_keep_recent", 1),
                         ("restore_skills_total_bytes", 0), ("restore_files_max", 0),
                         ("spill_threshold_bytes", 1), ("spill_preview_bytes", 1), ("think_first_steps", 0), ("wrap_up_steps", 0),
                         ("empty_max_retries", 0)):
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
            raise EvaError(Code.CONFIG_INVALID, f"{key} 必须是不小于 {minimum} 的整数", value=repr(v))
        values[key] = v
    for key in ("think_after_tools", "wrap_up_tools", "trim_tools"):
        tools = raw.get(key)
        if not isinstance(tools, list) or not all(isinstance(t, str) and t for t in tools):
            raise EvaError(Code.CONFIG_INVALID, f"{key} 必须是工具名列表（可为空）", value=repr(tools))
        values[key] = tuple(tools)
    if values["compact_keep_tokens"] >= values["compact_trigger_tokens"]:  # 否则压缩完马上又触发
        raise EvaError(Code.CONFIG_INVALID, "compact_keep_tokens 必须小于 compact_trigger_tokens")
    if values["trim_trigger_tokens"] >= values["compact_trigger_tokens"]:  # trim 是压缩前的便宜一步
        raise EvaError(Code.CONFIG_INVALID, "trim_trigger_tokens 必须小于 compact_trigger_tokens")
    if values["trim_min_freed_tokens"] >= values["trim_trigger_tokens"]:  # 否则永远清不出这么多
        raise EvaError(Code.CONFIG_INVALID, "trim_min_freed_tokens 必须小于 trim_trigger_tokens")
    if 2 * values["spill_preview_bytes"] + SPILL_NOTICE_RESERVE > values["spill_threshold_bytes"]:  # 替换后不得超过阈值
        raise EvaError(Code.CONFIG_INVALID, "两段预览加说明文字必须小于 spill_threshold_bytes",
                       reserve=SPILL_NOTICE_RESERVE)
    return LoopConfig(**values)


def check_tools(cfg: LoopConfig, registered: Iterable[str]) -> None:
    """名单里写错的工具名会让思考、收尾、清理静默不触发，装配时就抛错。"""
    unknown = sorted((set(cfg.think_after_tools) | set(cfg.wrap_up_tools) | set(cfg.trim_tools)) - set(registered))
    if unknown:
        raise EvaError(Code.CONFIG_INVALID, "think_after_tools、wrap_up_tools 或 trim_tools 里有未注册的工具", tools=unknown)


def check_window(cfg: LoopConfig, context_window: int) -> None:
    """触发线不小于模型窗口，压缩就永远来不及触发。窗口取主备中较小者。"""
    if cfg.compact_trigger_tokens >= context_window:
        raise EvaError(Code.CONFIG_INVALID, "compact_trigger_tokens 必须小于模型的上下文窗口",
                       compact_trigger_tokens=cfg.compact_trigger_tokens, context_window=context_window)
