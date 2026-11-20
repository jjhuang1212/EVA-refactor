"""一个 turn：多个 step，每个 step = 一次模型调用 + 它触发的工具调用。

循环只管"调模型 → 跑工具 → 收尾"，租户校验、出处校验等规则在 hook 里。所有状态都在 transcript，
进程里不留对话状态：任何 worker 重新进入同一会话都能接着跑。
"""
import json
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, List, Optional, Sequence, Tuple

from eva.contracts.errors import Code, EvaError
from eva.contracts.tooling import ToolCall, ToolContext
from eva.contracts.vocab import EventType, HookDecision, TurnEnd
from eva.hooks.base import Hook, run_pre_tool, run_stop
from eva.loop import compaction, view
from eva.loop.config import LoopConfig
from eva.loop.spill import preview as spill_preview
from eva.loop.scheduler import Item, Scheduler, aborted_payload, error_payload, partition
from eva.model.client import ModelClient, ModelResult
from eva.session.runner import Writer
from eva.tools.base import ToolRegistry

_SUMMARY_NOTE = "已达到本次任务的步数上限，不能再调用工具。请基于已有结果用文字总结：做了什么、得到什么、还缺什么。"
_WRAP_UP_NOTE = ("已达到本次任务的步数上限，现在收尾：只能使用 {tools}，把已经做好的结果保存并交付。做不到的不要硬做；"
                 "最后用文字如实说明做了什么、哪些没做完，没做完的不要说成已完成。")
# hook 的理由以用户消息的位置注入，不加说明模型会当成用户在发话，回复里去"回应"它
_STOP_NOTE = "【系统自动校验，不是用户说的话。按下面的问题改正后，直接给出完整的最终回答；回答面向用户，不要提这次校验，也不要回应这条提示。】"
# 这类错误码出自模型层和账本，日志本身还能写；存储和序号类错误说明日志写不了，直接抛
_LOGGED_ERRORS = ("E4", "E5", "E3004")


@dataclass
class TurnResult:
    reason: TurnEnd
    text: str
    steps: int
    cost_cny: Decimal
    options: Tuple[str, ...] = ()  # 停放的 ask_user 带的选项


def run_turn(writer: Writer, model: ModelClient, tools: ToolRegistry, hooks: Sequence[Hook], cfg: LoopConfig,
             system_prompt: str, cancel: threading.Event, spill: Optional[Callable[[str, str], str]] = None,
             restore: Optional[compaction.Restore] = None) -> TurnResult:
    """跑一个 turn。调用前用户消息应已写进日志。模型层或账本抛错时先写 error 事件再原样抛出。
    restore 取压缩时要重新放回视图的信息，见 compaction.compact。"""
    writer.recover_tool_calls(exempt_tools=tools.parking_names())
    compaction.close_dangling(writer)
    writer.append(EventType.TURN_START.value, {})
    run = _Run(writer, model, tools, hooks, cfg, system_prompt, cancel, spill, restore)
    try:
        reason = run.loop()
    except EvaError as e:
        if e.code.value.startswith(_LOGGED_ERRORS):
            writer.append(EventType.ERROR.value, e.to_dict())
            run.end(TurnEnd.ERROR, code=e.code.value)
        raise
    finally:
        run.scheduler.close()
    return run.end(reason)


class _Run:
    def __init__(self, writer, model, tools, hooks, cfg, system_prompt, cancel, spill, restore=None):
        self.spill, self.restore = spill, restore
        self.w, self.model, self.tools, self.hooks, self.cfg = writer, model, tools, hooks, cfg
        self.system_prompt, self.cancel = system_prompt, cancel
        self.steps, self.cost, self.text = 0, Decimal(0), ""
        self.options: Tuple[str, ...] = ()
        self.reset_reply: Optional[str] = None  # 本 turn 里成功的重置类工具给用户的回复
        self.prev_tools = frozenset()  # 上一步模型要调用的工具名
        self.scheduler = Scheduler(cfg.max_parallel_tools)

    def end(self, reason: TurnEnd, **extra) -> TurnResult:
        self.w.append(EventType.TURN_END.value,
                      {"reason": reason.value, "steps": self.steps, "cost_cny": str(self.cost), **extra})
        return TurnResult(reason, self.text, self.steps, self.cost, self.options)

    def ctx(self, call_id: str) -> ToolContext:
        return ToolContext(self.w.tenant_id, self.w.session_id, call_id, self.cancel)

    def loop(self) -> TurnEnd:
        gate_retries, empty_retries = 0, 0
        while True:
            if self.cancel.is_set():
                return TurnEnd.CANCELLED
            wrap_steps = self.cfg.wrap_up_steps if self.cfg.wrap_up_tools else 0
            last_chance = self.steps >= self.cfg.max_steps + wrap_steps
            wrap_up = not last_chance and self.steps >= self.cfg.max_steps
            if wrap_up and self.steps == self.cfg.max_steps:
                self.w.append(EventType.USER_MESSAGE.value,
                              {"source": "loop", "text": _WRAP_UP_NOTE.format(tools="、".join(self.cfg.wrap_up_tools))})
            elif last_chance:
                self.w.append(EventType.USER_MESSAGE.value, {"source": "loop", "text": _SUMMARY_NOTE})
            schemas = None if last_chance else (self.tools.schemas() or None)
            if wrap_up:
                schemas = [s for s in schemas or [] if s["function"]["name"] in self.cfg.wrap_up_tools] or None
            r = self.ask_model(schemas,
                               thinking=self.steps < self.cfg.think_first_steps
                               or bool(self.prev_tools.intersection(self.cfg.think_after_tools)))
            self.prev_tools = frozenset(c["name"] for c in r.tool_calls)
            self.steps += 1
            self.cost += r.cost_cny
            self.text = r.content or ""
            if r.reasoning:  # 写在 assistant_message 之前，它和随后的 tool_call 仍然相邻
                self.w.append(EventType.REASONING.value,
                              {"text": r.reasoning, "chars": r.reasoning_chars, "model": r.model})
            self.w.append(EventType.ASSISTANT_MESSAGE.value, {
                "content": r.content, "tool_calls": r.tool_calls, "model": r.model,
                "finish_reason": r.finish_reason, "cost_cny": str(r.cost_cny),
                "tokens_in": r.tokens_in, "tokens_out": r.tokens_out, "seconds": r.seconds})
            for c in r.tool_calls:
                self.w.append(EventType.TOOL_CALL.value,
                              {"call_id": c["id"], "name": c["name"], "arguments": c["arguments"]})
            if last_chance:  # 禁用了工具还是返回了调用，不执行，补结果让日志合法
                for c in r.tool_calls:
                    self.result(aborted_payload(c["id"]))
                return TurnEnd.MAX_STEPS
            if r.tool_calls:
                if r.finish_reason == "length":  # 参数可能是残缺的，整批不执行，让模型重发
                    for c in r.tool_calls:
                        self.result(error_payload(c["id"], EvaError(
                            Code.TOOL_CALL_TRUNCATED, "输出被截断，这批工具调用都没有执行，请重新发起")))
                else:
                    calls = r.tool_calls
                    if wrap_up:  # 没开放的工具模型也可能硬调，不执行
                        for c in calls:
                            if c["name"] not in self.cfg.wrap_up_tools:
                                self.result(error_payload(c["id"], EvaError(
                                    Code.TOOL_DENIED, "收尾阶段只能使用：" + "、".join(self.cfg.wrap_up_tools), tool=c["name"])))
                        calls = [c for c in calls if c["name"] in self.cfg.wrap_up_tools]
                    parked = self.dispatch(calls) if calls else None
                    if parked is not None:
                        self.text = parked.call.arguments["question"]
                        self.options = tuple(parked.call.arguments.get("options") or ())
                        return TurnEnd.WAITING_USER
                    if self.reset_reply is not None:
                        self.w.append(EventType.CONTEXT_RESET.value, {"reason": "new_topic"})
                        self.text = self.reset_reply
                        return TurnEnd.COMPLETED
                continue
            if r.finish_reason == "length":
                return TurnEnd.OUTPUT_LIMIT
            if not self.text.strip():
                if empty_retries >= self.cfg.empty_max_retries:
                    return TurnEnd.EMPTY_RESPONSE
                empty_retries += 1  # 空的这次已写进日志（排查用），视图不带它，原样再调一次
                continue
            stop, records = run_stop(self.hooks, self.ctx(""), self.text)
            self.log_hooks("stop", "", records)
            if not stop.block:
                return TurnEnd.COMPLETED
            if gate_retries >= self.cfg.gate_max_retries:
                return TurnEnd.GATE_FAILED
            gate_retries += 1
            self.w.append(EventType.USER_MESSAGE.value, {"source": "stop_hook", "text": f"{_STOP_NOTE}\n{stop.reason}"})

    def ask_model(self, schemas, thinking: bool = False) -> ModelResult:
        """调一次模型。调用前视图过大先清旧工具结果，还大再压缩；厂商报上下文溢出时压缩后重试一次，仍溢出就把错误抛出去。"""
        overhead = len(self.system_prompt) + len(json.dumps(schemas or [], ensure_ascii=False))
        for attempt in (0, 1):
            events = self.w.live()
            if attempt == 0:
                events = self.shrink(events, overhead)
            try:
                return self.model.call(self.w.session_id, "turn", view.derive(events, self.system_prompt),
                                       tools=schemas, thinking=thinking)
            except EvaError as e:
                if e.code is not Code.MODEL_CONTEXT_OVERFLOW or attempt == 1:
                    raise
                self.compact("overflow")

    def shrink(self, events: list, overhead: int) -> list:
        """视图太大时先便宜后昂贵：先清旧工具结果（不调模型），仍超线再整段压缩。压缩连续失败后不再由压力触发。"""
        cfg = self.cfg
        if compaction.pressure(events, overhead) >= cfg.trim_trigger_tokens and compaction.trim(self.w, events, cfg):
            return self.w.live()  # 清完没有真实用量，估算是上界会偏高；等下一步有了 trim 后的真实用量再判断要不要压缩
        if compaction.pressure(events, overhead) >= cfg.compact_trigger_tokens \
                and not compaction.breaker_open(events, cfg.compact_max_failures):
            self.compact("pressure")
            events = self.w.live()
        return events

    def compact(self, reason: str) -> None:
        self.cost += compaction.compact(self.w, self.model, self.system_prompt, self.cfg.compact_keep_tokens, reason,
                                        self.restore)

    def result(self, payload: dict, tool=None) -> None:
        if tool is not None and tool.resets and not payload.get("is_error") and isinstance(payload.get("content"), str):
            self.reset_reply = payload["content"]
        if tool is not None and tool.spills and self.spill is not None:
            payload = self.maybe_spill(payload)
        self.w.append(EventType.TOOL_RESULT.value, payload)

    def maybe_spill(self, payload: dict) -> dict:
        """纯文本结果超过阈值就溢出。存储失败时显式降级：保留原结果，在日志里记下失败的错误码。"""
        content = payload.get("content")
        if payload.get("is_error") or not isinstance(content, str):
            return payload
        data = content.encode("utf-8")
        if len(data) <= self.cfg.spill_threshold_bytes:
            return payload
        try:
            key = self.spill(self.w.session_id, content)
        except EvaError as e:
            return {**payload, "spill_failed": e.code.value}
        return {**payload, "content": spill_preview(data, key, self.cfg.spill_preview_bytes),
                "spilled": {"key": key, "bytes": len(data)}}

    def prepare(self, c: dict) -> Item:
        """校验工具名和参数，再过 pre_tool hook。任何一步不通过都直接得到结构化结果，不执行。"""
        item = Item(ToolCall(c["id"], c["name"], {}), self.ctx(c["id"]))
        try:
            item.tool = self.tools.get(c["name"])
            args = self.tools.parse_arguments(item.tool, c["arguments"])
        except EvaError as e:
            item.result = error_payload(c["id"], e)
            return item
        item.call = ToolCall(c["id"], c["name"], args)
        decision, records = run_pre_tool(self.hooks, item.ctx, item.call)
        self.log_hooks("pre_tool", c["id"], records)
        if decision.decision is not HookDecision.ALLOW:
            why = decision.reason or ("需要用户确认，当前无法询问" if decision.decision is HookDecision.ASK else "")
            item.result = error_payload(c["id"], EvaError(
                Code.TOOL_DENIED, why, tool=c["name"], decision=decision.decision.value))
        return item

    def log_hooks(self, point: str, call_id: str, records: List[dict]) -> None:
        if not self.hooks:
            return
        self.w.append(EventType.HOOK.value, {"phase": "invoked", "point": point, "call_id": call_id})
        for rec in records:
            self.w.append(EventType.HOOK.value, {"phase": "result", "call_id": call_id, **rec})

    def dispatch(self, calls: List[dict]) -> Optional[Item]:
        """执行这批调用。返回被停放的调用（有就不写结果，等用户回答），没有返回 None。一步只停放第一个。"""
        parked, todo = None, []
        for it in [self.prepare(c) for c in calls]:
            if it.result is None and it.tool.parks:
                if parked is None:
                    parked = it
                    continue
                it.result = error_payload(it.call.id, EvaError(
                    Code.TOOL_DENIED, "一次只能向用户提一个问题", tool=it.call.name))
            todo.append(it)
        batches = partition(todo)
        for n, batch in enumerate(batches):
            if self.cancel.is_set():
                for it in [i for b in batches[n:] for i in b] + ([parked] if parked else []):
                    self.result(aborted_payload(it.call.id))
                return None
            for it in batch:
                if it.result is None:
                    self.w.append(EventType.TOOL_START.value, {"call_id": it.call.id})
            for it, payload in zip(batch, self.scheduler.execute(batch)):
                self.result(payload, it.tool)
        return parked



