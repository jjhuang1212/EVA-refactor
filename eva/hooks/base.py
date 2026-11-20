"""hook：租户校验、预算预扣、出处校验等规则放在这里，不靠模型自觉。

多个 hook 命中同一点时权限取最严（deny > ask > allow），第一个理由保留。
hook 抛 EvaError 视为对该调用的拒绝并带上错误码；其他异常是 bug，原样抛出。
"""
import time
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from eva.contracts.errors import EvaError
from eva.contracts.tooling import ToolCall, ToolContext
from eva.contracts.vocab import HookDecision

_STRICTNESS = {HookDecision.ALLOW: 0, HookDecision.ASK: 1, HookDecision.DENY: 2}


@dataclass(frozen=True)
class HookResult:
    decision: HookDecision = HookDecision.ALLOW
    reason: str = ""


@dataclass(frozen=True)
class StopResult:
    block: bool = False  # True 表示不让结束，reason 作为新输入注入
    reason: str = ""


class Hook:
    """基类，默认全部放行。子类只覆盖关心的点。"""
    name = "hook"

    def pre_tool(self, ctx: ToolContext, call: ToolCall) -> HookResult:
        return HookResult()

    def stop(self, ctx: ToolContext, final_text: str) -> StopResult:
        return StopResult()


def run_pre_tool(hooks: Sequence[Hook], ctx: ToolContext, call: ToolCall) -> Tuple[HookResult, List[dict]]:
    """返回合并后的决定，以及每个 hook 的调用记录（决定、耗时、错误摘要），由循环写进日志。"""
    final, records = HookResult(), []
    for h in hooks:
        start, error = time.monotonic(), None
        try:
            r = h.pre_tool(ctx, call)
        except EvaError as e:
            r, error = HookResult(HookDecision.DENY, e.message or e.code.name), e.to_dict()
        records.append({"hook": h.name, "point": "pre_tool", "decision": r.decision.value, "reason": r.reason,
                        "ms": round((time.monotonic() - start) * 1000), "error": error})
        if _STRICTNESS[r.decision] > _STRICTNESS[final.decision]:
            final = r
    return final, records


def run_stop(hooks: Sequence[Hook], ctx: ToolContext, final_text: str) -> Tuple[StopResult, List[dict]]:
    """任一 hook 要求阻止结束就阻止；理由取第一个阻止者的。"""
    final, records = StopResult(), []
    for h in hooks:
        start = time.monotonic()
        r = h.stop(ctx, final_text)
        records.append({"hook": h.name, "point": "stop", "block": r.block, "reason": r.reason,
                        "ms": round((time.monotonic() - start) * 1000)})
        if r.block and not final.block:
            final = r
    return final, records
