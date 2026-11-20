"""transcript 事件对应的一行运行日志。

每写一条事件就打一行，turn、模型、工具、hook、压缩、错误都经过 Writer.append 这一个点，日志和 transcript 不会对不上。
每个 EventType 对应一个 _<事件名> 方法，返回 (级别, 消息, 字段)；新增事件类型而没有方法，测试会红。
"""
import json
import logging
import time
from typing import Optional, Tuple

from eva.common import logs
from eva.contracts.vocab import EventType, HookDecision, TurnEnd

log = logging.getLogger(__name__)

_WARN_ENDS = {TurnEnd.MAX_STEPS.value, TurnEnd.GATE_FAILED.value, TurnEnd.OUTPUT_LIMIT.value,
              TurnEnd.EMPTY_RESPONSE.value}
Line = Tuple[int, str, dict]


def _short(call_id) -> Optional[str]:
    return str(call_id)[:8] if call_id else None


class Trace:
    """一个 Writer 一份：记下调用 ID 对应的工具名和开始时间，让结果行能带上工具名和耗时。"""

    def __init__(self):
        self._names, self._started, self._turn_at = {}, {}, None

    def record(self, kind: EventType, seq: int, payload: dict) -> None:
        level, msg, fields = getattr(self, "_" + kind.value)(payload)
        logs.emit(log, level, msg, seq=seq, **fields)

    def _seconds(self, since: Optional[float]) -> Optional[float]:
        return None if since is None else round(time.monotonic() - since, 1)

    def _turn_start(self, p: dict) -> Line:
        self._turn_at = time.monotonic()
        return logging.INFO, "回合开始", {}

    def _turn_end(self, p: dict) -> Line:
        reason = p.get("reason")
        level = logging.ERROR if reason == TurnEnd.ERROR.value else logging.WARNING if reason in _WARN_ENDS else logging.INFO
        return level, "回合结束", {"reason": reason, "steps": p.get("steps"), "cost_cny": p.get("cost_cny"),
                               "code": p.get("code"), "seconds": self._seconds(self._turn_at)}

    def _user_message(self, p: dict) -> Line:
        text = p.get("text") or ""
        return logging.INFO, "用户消息", {"source": p.get("source", "user"), "chars": len(text),
                                       "preview": logs.preview(text)}

    def _assistant_message(self, p: dict) -> Line:
        content = p.get("content") or ""
        calls = [c.get("name") for c in p.get("tool_calls") or []]
        return logging.INFO, "模型回复", {
            "model": p.get("model"), "tokens_in": p.get("tokens_in"), "tokens_out": p.get("tokens_out"),
            "cost_cny": p.get("cost_cny"), "finish": p.get("finish_reason"), "chars": len(content),
            "tools": ",".join(calls), "preview": logs.preview(content)}

    def _reasoning(self, p: dict) -> Line:
        return logging.INFO, "模型思考", {"model": p.get("model"), "chars": p.get("chars"),
                                       "preview": logs.preview(p.get("text") or "")}

    def _tool_call(self, p: dict) -> Line:
        self._names[p.get("call_id")] = p.get("name")
        args = p.get("arguments")
        return logging.INFO, "工具调用", {"tool": p.get("name"), "call": _short(p.get("call_id")),
                                      "args": logs.preview(args if isinstance(args, str) else _dump(args))}

    def _tool_start(self, p: dict) -> Line:
        self._started[p.get("call_id")] = time.monotonic()
        return logging.DEBUG, "工具开始", {"tool": self._names.get(p.get("call_id")), "call": _short(p.get("call_id"))}

    def _tool_result(self, p: dict) -> Line:
        call_id, content = p.get("call_id"), p.get("content")
        failed = bool(p.get("is_error"))
        return (logging.WARNING if failed else logging.INFO), "工具结果", {
            "tool": self._names.get(call_id), "call": _short(call_id),
            "code": (p.get("error") or {}).get("code"), "chars": len(content) if isinstance(content, str) else None,
            "seconds": self._seconds(self._started.get(call_id)), "preview": logs.preview(content)}

    def _compaction_start(self, p: dict) -> Line:
        return logging.INFO, "压缩开始", {"reason": p.get("reason"), "cut_seq": p.get("cut_seq")}

    def _compaction_end(self, p: dict) -> Line:
        failed = p.get("status") != "ok"
        return (logging.WARNING if failed else logging.INFO), "压缩结束", {
            "status": p.get("status"), "reason": p.get("reason"), "code": p.get("code"), "model": p.get("model"),
            "cost_cny": p.get("cost_cny"), "preview": logs.preview(p.get("summary"))}

    def _trim(self, p: dict) -> Line:
        return logging.INFO, "清理旧工具结果", {"cut_seq": p.get("cut_seq"), "cleared": p.get("cleared"),
                                           "freed_tokens": p.get("freed_tokens")}

    def _hook(self, p: dict) -> Line:
        if p.get("phase") == "invoked":
            return logging.DEBUG, "hook 调用", {"point": p.get("point"), "call": _short(p.get("call_id"))}
        stricter = p.get("decision") in (HookDecision.DENY.value, HookDecision.ASK.value) or p.get("block")
        return (logging.WARNING if stricter else logging.DEBUG), "hook 结果", {
            "hook": p.get("hook"), "point": p.get("point"), "decision": p.get("decision"), "block": p.get("block"),
            "reason": p.get("reason"), "ms": p.get("ms"), "call": _short(p.get("call_id")),
            "code": (p.get("error") or {}).get("code")}

    def _subagent_start(self, p: dict) -> Line:
        return logging.INFO, "子 agent 开始", {}

    def _subagent_end(self, p: dict) -> Line:
        return logging.INFO, "子 agent 结束", {}

    def _context_reset(self, p: dict) -> Line:
        return logging.INFO, "上下文重置", {}

    def _error(self, p: dict) -> Line:
        return logging.ERROR, p.get("message") or "错误", {
            "code": p.get("code"), "name": p.get("name"), "retryable": p.get("retryable"), "details": p.get("details")}


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
