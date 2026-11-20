"""上下文压缩：只改模型视图，不改日志。分两步，先便宜后昂贵。

trim：旧的工具结果换成占位说明，不调模型。写一条 trim 事件记下切点和清理的工具名单，视图据此现算。
一次清一批，不逐轮滑动，前缀才稳定，隐式缓存不会每轮失效。
压缩：压缩开始写 compaction_start（兼作该会话的压缩锁），结束写 compaction_end（成功带摘要、切点和压缩时重新取的
信息，失败带原因）。失败要关闭：摘要为空、被截断、模型调用出错，都不落摘要，只留一条失败的结束事件。
"""
import json
from decimal import Decimal
from typing import Callable, List, Optional

from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import EventType
from eva.loop import view
from eva.model.client import ModelClient
from eva.session.runner import Writer

TEMPLATE = (
    "请把上面的对话压缩成一份摘要，供后续继续工作使用。只输出摘要正文，按下面的小节写，没有内容的小节写“无”：\n"
    "## 用户目标\n## 已确认的实体和对齐结果\n"
    "## 已取得的证据及出处（文件、页码、章节标题、元素类型、表号或图号、来源 ID 必须原样抄录，不得改写、合并或省略）\n"
    "## 待办事项\n## 用户偏好和纠正"
)
_IN_VIEW = (EventType.USER_MESSAGE.value, EventType.ASSISTANT_MESSAGE.value, EventType.TOOL_RESULT.value)
_CUT_AT = (EventType.USER_MESSAGE.value, EventType.ASSISTANT_MESSAGE.value)


def _size(e: dict, cleared=frozenset()) -> int:
    """没有分词器，按一字符一 token 取上界，与模型层预扣的估法一致。被 trim 清掉的工具结果只剩占位说明。"""
    if e["event_type"] not in _IN_VIEW:
        return 0
    if e["event_type"] == EventType.TOOL_RESULT.value and e["payload"].get("call_id") in cleared:
        return len(view.TRIMMED)
    return len(json.dumps(e["payload"], ensure_ascii=False))


def pressure(events: List[dict], overhead_chars: int) -> int:
    """当前视图大概多少 token。优先用压缩或 trim 之后最近一次调用的真实用量，再加此后新增事件的估算；
    之前的调用用量包含已被摘要代替或已被清掉的内容，不可信。"""
    vis, summary, end_seq, end_event = view.visible(events)
    trims = view.trim_events(vis)
    floor = max(end_seq, trims[-1]["seq"] if trims else -1)
    last = None
    for i, e in enumerate(vis):
        p = e["payload"]
        if e["event_type"] == EventType.ASSISTANT_MESSAGE.value and e["seq"] > floor \
                and isinstance(p.get("tokens_in"), int) and isinstance(p.get("tokens_out"), int):
            last = i
    if last is not None:
        p = vis[last]["payload"]
        return p["tokens_in"] + p["tokens_out"] + sum(_size(e) for e in vis[last + 1:])
    cleared = view.trimmed_call_ids(vis)
    return (overhead_chars + len(summary or "") + len(view.restored_text(end_event) or "")
            + sum(_size(e, cleared) for e in vis))


def plan_trim(vis: List[dict], cfg) -> Optional[dict]:
    """要清哪些：名单里的工具，除最近 trim_keep_recent 次调用以外的旧结果。能省的不到 trim_min_freed_tokens 返回 None。"""
    tools, cleared = set(cfg.trim_tools), view.trimmed_call_ids(vis)
    calls = [(e["seq"], c["id"]) for e in vis if e["event_type"] == EventType.ASSISTANT_MESSAGE.value
             for c in e["payload"].get("tool_calls") or [] if c.get("name") in tools]
    if len(calls) <= cfg.trim_keep_recent:
        return None
    cut = calls[-cfg.trim_keep_recent][0]
    sizes = {e["payload"].get("call_id"): _size(e) for e in vis if e["event_type"] == EventType.TOOL_RESULT.value}
    ids = [cid for seq, cid in calls if seq < cut and cid not in cleared]
    freed = sum(max(sizes.get(cid, 0) - len(view.TRIMMED), 0) for cid in ids)
    if freed < cfg.trim_min_freed_tokens:
        return None
    return {"cut_seq": cut, "tools": sorted(tools), "cleared": len(ids), "freed_tokens": freed}


def trim(w: Writer, events: List[dict], cfg) -> bool:
    """写一条 trim 事件，返回是否清理了。不调模型。"""
    plan = plan_trim(view.visible(events)[0], cfg)
    if plan is None:
        return False
    w.append(EventType.TRIM.value, plan)
    return True


def breaker_open(events: List[dict], max_failures: int) -> bool:
    """最近连续 max_failures 次压缩都失败：压力触发的压缩不再试，免得每一步都白花一次摘要调用。
    成功一次或上下文重置就清零。溢出触发的压缩不受它限制，那是最后一招。"""
    failures = 0
    for e in reversed(events):
        if e["event_type"] == EventType.CONTEXT_RESET.value:
            break
        if e["event_type"] == EventType.COMPACTION_END.value:
            if e["payload"].get("status") == "ok":
                break
            failures += 1
    return failures >= max_failures


def choose_cut(vis: List[dict], keep_tokens: int) -> int:
    """返回切点在 vis 里的下标：此前的事件被摘要，此后的原样保留。
    切点只落在用户或助手消息的开头，所以助手的工具调用和它的结果不会被拆开。
    在尾部不超过 keep_tokens 的前提下尽量多保留；头部至少要有一条消息可摘要。"""
    cands = [i for i, e in enumerate(vis) if e["event_type"] in _CUT_AT]
    if len(cands) < 2:
        raise EvaError(Code.COMPACTION_FAILED, "没有可压缩的内容", messages=len(cands))
    tail, suffix, cleared = 0, {}, view.trimmed_call_ids(vis)
    for i in range(len(vis) - 1, -1, -1):
        tail += _size(vis[i], cleared)
        suffix[i] = tail
    for i in cands[1:]:
        if suffix[i] <= keep_tokens:
            return i
    return cands[-1]


def close_dangling(w: Writer) -> None:
    """上次运行在压缩中途崩溃：有 start 没有 end。补一条失败的 end，释放压缩锁。"""
    open_start = False
    for e in w.live():
        if e["event_type"] == EventType.COMPACTION_START.value:
            open_start = True
        elif e["event_type"] == EventType.COMPACTION_END.value:
            open_start = False
    if open_start:
        w.append(EventType.COMPACTION_END.value, {"status": "failed", "reason": "interrupted"})


Restore = Callable[[List[dict], int], dict]


def compact(w: Writer, model: ModelClient, system_prompt: str, keep_tokens: int, reason: str,
            restore: Optional[Restore] = None) -> Decimal:
    """压缩并返回这次摘要调用的花费。失败抛错误码，日志里留一条失败的结束事件。
    restore 取压缩时要重新放回视图的信息（事件列表和切点 -> attachments），结果写进 compaction_end。"""
    events = w.live()
    vis, _, _, end_event = view.visible(events)
    cut_seq = vis[choose_cut(vis, keep_tokens)]["seq"]
    w.append(EventType.COMPACTION_START.value, {"cut_seq": cut_seq, "reason": reason})
    try:
        attachments = restore(events, cut_seq) if restore else None
    except EvaError as e:
        w.append(EventType.COMPACTION_END.value, {"status": "failed", "reason": "restore_error", "code": e.code.value})
        raise

    def fail(why: str, **details) -> EvaError:
        w.append(EventType.COMPACTION_END.value, {"status": "failed", "reason": why, **details})
        return EvaError(Code.COMPACTION_FAILED, why, **details)

    head = [e for e in events if e["seq"] < cut_seq or e is end_event]  # 上次的摘要也要进来，它代替了更早的、这里读不到的事件
    messages = view.derive(head, system_prompt) + [{"role": "user", "content": TEMPLATE}]
    try:
        r = model.call(w.session_id, "compaction", messages)
    except EvaError as e:
        w.append(EventType.COMPACTION_END.value, {"status": "failed", "reason": "model_error", "code": e.code.value})
        raise
    summary = (r.content or "").strip()
    if r.finish_reason == "length":
        raise fail("摘要被截断")
    if not summary:
        raise fail("摘要为空")
    done = {"status": "ok", "cut_seq": cut_seq, "summary": summary, "model": r.model, "cost_cny": str(r.cost_cny)}
    if attachments is not None:
        done["attachments"] = attachments
    w.append(EventType.COMPACTION_END.value, done)
    return r.cost_cny

