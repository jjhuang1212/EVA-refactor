"""交付门禁：模型想结束时，本 turn 已进入某条流程（调用过它的入口工具）却没交付该流程必须交的文件，就不让结束。

模型有时在流程中途用一段文字"预告下一步"就停下，对循环来说那就是最终回答。门禁把它拦回来，拦的次数有上限，
超过上限放行，由回答向用户如实说明没做完（架构设计 7.7）。状态都在日志里，重启后不会重复拦。
"""
import json
import re
from typing import Callable, Dict

from eva.contracts.tooling import ToolContext
from eva.contracts.vocab import EventType
from eva.contracts.vocab import HookDecision
from eva.contracts.tooling import ToolCall
from eva.hooks.base import Hook, HookResult, StopResult
from eva.hooks.config import DeliveryGate
from eva.storage import db
from eva.storage.transcript import Transcript


class DeliveryGateHook(Hook):
    name = "delivery_gate"

    def __init__(self, gates: Dict[str, DeliveryGate], open_tx: Callable = db.transaction):
        self._gates, self._open_tx = gates, open_tx

    @staticmethod
    def _calls(events):
        """事件里成功的工具调用：[(工具名, 参数 JSON 串, 结果载荷)]。"""
        calls = {e["payload"]["call_id"]: e["payload"] for e in events if e["event_type"] == EventType.TOOL_CALL.value}
        return [(calls[e["payload"]["call_id"]]["name"], calls[e["payload"]["call_id"]].get("arguments", ""), e["payload"]) for e in events
                if e["event_type"] == EventType.TOOL_RESULT.value and not e["payload"].get("is_error") and e["payload"].get("call_id") in calls]

    def _turn(self, ctx: ToolContext):
        """本 turn 里成功的工具调用，和本 turn 的全部事件。"""
        with self._open_tx() as conn:
            events = Transcript(conn, ctx.tenant_id).current_turn(ctx.session_id)
        return self._calls(events), events

    def _session(self, ctx: ToolContext):
        """最近一次上下文重置以来成功的工具调用。流程中间向用户提问会让 turn 断开，加载 skill 可能发生在前一个 turn；
        重置之前读过的 skill 已不在模型视图里，不算。"""
        with self._open_tx() as conn:
            return self._calls(Transcript(conn, ctx.tenant_id).read_since_reset(ctx.session_id))

    def pre_tool(self, ctx: ToolContext, call: ToolCall) -> HookResult:
        """进入了流程却整个会话都没加载过它的 skill，就不让交付：模型自己手写的文件会占掉企微的下发条数，也不是 build 的产出。"""
        gates = [g for g in self._gates.values() if g.skill]
        if call.name != "deliver_file" or not gates:
            return HookResult()
        results, _ = self._turn(ctx)
        session = None
        for gate in gates:
            if not any(n == gate.entered_by for n, _, _ in results):
                continue
            session = session if session is not None else self._session(ctx)
            if not any(n == "load_skill" and _arg(a, "name") == gate.skill for n, a, _ in session):
                return HookResult(HookDecision.DENY, f"这是 {gate.skill} 流程，先 load_skill {gate.skill}，按它的步骤用脚本生成文件再交付，不要交付自己手写的文件。")
        return HookResult()

    def stop(self, ctx: ToolContext, final_text: str) -> StopResult:
        if not self._gates:
            return StopResult()
        results3, events = self._turn(ctx)
        results = [(n, p) for n, _, p in results3]
        delivered = [_filename(p) for n, p in results if n == "deliver_file"]
        saved = [_filename(p) for n, p in results if n == "write_file"]
        for gate in self._gates.values():
            missing = [x for x in gate.must_deliver if not any(re.search(x, f) for f in delivered)]
            missing += [x for x in gate.must_save if not any(re.search(x, f) for f in saved)]
            if not any(n == gate.entered_by for n, _ in results) or not missing:
                continue
            blocked = sum(1 for e in events if e["event_type"] == EventType.USER_MESSAGE.value and gate.message in str(e["payload"].get("text")))
            if blocked < gate.max_blocks:
                return StopResult(True, gate.message + "还缺：" + "；".join(missing))
        return StopResult()


def _arg(arguments, key: str):
    try:
        return json.loads(arguments).get(key)
    except (TypeError, ValueError, AttributeError):
        return None


def _filename(payload: dict) -> str:
    """deliver_file 成功时的结果是 JSON，带 filename。"""
    return json.loads(payload["content"])["filename"]
