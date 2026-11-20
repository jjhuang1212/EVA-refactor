"""从 transcript 推导模型看到的消息列表。日志是事实来源，视图每次现算，不另存。"""
import json
from typing import Dict, List, Optional

from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import EventType


def _corrupt(event: dict, why: str) -> EvaError:
    return EvaError(Code.STORAGE_ERROR, f"日志事件无法还原成消息：{why}", seq=event["seq"],
                    event_type=event["event_type"])


def _tool_content(payload: dict) -> str:
    if isinstance(payload.get("content"), str):
        return payload["content"]
    # 合成结果（崩溃恢复、取消）没有 content，把状态原样给模型
    return json.dumps({k: v for k, v in payload.items() if k != "call_id"}, ensure_ascii=False)


SUMMARY_PREFIX = "以下是此前对话的摘要，原文已省略，出处锚点保持原样：\n"
RESTORED_PREFIX = "以下是压缩时从系统里重新取的信息，供继续工作使用：\n\n"
TRIMMED = "[旧工具结果已清理，需要时重新调用工具]"


def trim_events(events: List[dict]) -> List[dict]:
    return [e for e in events if e["event_type"] == EventType.TRIM.value]


def trimmed_call_ids(events: List[dict]) -> frozenset:
    """视图里结果内容已被清掉的工具调用 ID：每条 trim 事件清掉它切点之前、它记录的那些工具的调用。
    清掉的名单取自事件本身，配置后来改了，回放出来的视图也不变。"""
    out = set()
    for t in trim_events(events):
        cut, tools = t["payload"].get("cut_seq"), t["payload"].get("tools")
        if isinstance(cut, bool) or not isinstance(cut, int) or not isinstance(tools, list):
            raise _corrupt(t, "trim 缺 cut_seq 或 tools")
        for e in events:
            if e["event_type"] == EventType.ASSISTANT_MESSAGE.value and e["seq"] < cut:
                out.update(c["id"] for c in e["payload"].get("tool_calls") or [] if c.get("name") in tools)
    return frozenset(out)


def restored_text(end_event: Optional[dict]) -> Optional[str]:
    """压缩结束事件里 attachments 渲染成的一条消息，没有可放的返回 None。"""
    att = None if end_event is None else end_event["payload"].get("attachments")
    if att is None:
        return None
    try:
        parts = []
        if att["skills"]:
            parts.append("本会话加载过的 skill，以下是现在的最新版本：\n\n" + "\n\n".join(
                f"## {s['name']}\n{s['text']}" for s in att["skills"]))
        if att["skills_omitted"]:
            parts.append("以下 skill 的正文因篇幅没有放回，需要时重新 load_skill：" + "、".join(att["skills_omitted"]))
        if att["skills_missing"]:
            parts.append("以下 skill 现在已经不存在：" + "、".join(att["skills_missing"]))
        if att["files"]:
            parts.append("本租户登记的文件（最近的在前）：\n" + "\n".join(
                f"- {f['filename']}（file_id：{f['file_id']}，{f['mime']}，{f['bytes']} 字节）" for f in att["files"]))
    except (KeyError, TypeError):
        raise _corrupt(end_event, "compaction_end 的 attachments 格式不对") from None
    return RESTORED_PREFIX + "\n\n".join(parts) if parts else None


def ok_compaction(events: List[dict]):
    """最近一次成功的压缩结束事件的下标，没有返回 None。"""
    found = None
    for i, e in enumerate(events):
        if e["event_type"] == EventType.COMPACTION_END.value and e["payload"].get("status") == "ok":
            found = i
    return found


def visible(events: List[dict]):
    """模型视图覆盖的事件、要放在最前面的摘要（没有压缩过为 None）、压缩结束事件的序号（没有为 -1）、
    压缩结束事件本身（没有为 None）。
    最近一次 context_reset 之前的丢弃；其后如果有成功的压缩，切点之前的事件由摘要代替。
    """
    start = 0
    for i, e in enumerate(events):
        if e["event_type"] == EventType.CONTEXT_RESET.value:
            start = i + 1
    events = events[start:]
    done = ok_compaction(events)
    if done is None:
        return events, None, -1, None
    p = events[done]["payload"]
    if not isinstance(p.get("summary"), str) or not isinstance(p.get("cut_seq"), int):
        raise _corrupt(events[done], "compaction_end 缺 summary 或 cut_seq")
    return [e for e in events if e["seq"] >= p["cut_seq"]], p["summary"], events[done]["seq"], events[done]


def derive(events: List[dict], system_prompt: str) -> List[dict]:
    """最近一次 context_reset 之后的事件还原成消息，压缩过的部分换成摘要，被 trim 的旧工具结果换成占位说明。
    assistant 的每个工具调用都必须已有结果，否则日志不完整。"""
    events, summary, _, end_event = visible(events)
    cleared = trimmed_call_ids(events)
    results: Dict[str, dict] = {}
    for e in events:
        if e["event_type"] == EventType.TOOL_RESULT.value and isinstance(e["payload"].get("call_id"), str):
            results.setdefault(e["payload"]["call_id"], e["payload"])

    messages = [{"role": "system", "content": system_prompt}]
    if summary is not None:
        messages.append({"role": "user", "content": SUMMARY_PREFIX + summary})
        restored = restored_text(end_event)
        if restored is not None:
            messages.append({"role": "user", "content": restored})
    for e in events:
        kind, p = e["event_type"], e["payload"]
        if kind == EventType.USER_MESSAGE.value:
            if not isinstance(p.get("text"), str):
                raise _corrupt(e, "user_message 缺 text")
            messages.append({"role": "user", "content": p["text"]})
        elif kind == EventType.ASSISTANT_MESSAGE.value:
            calls = p.get("tool_calls") or []
            if not calls and not (p.get("content") or "").strip():
                continue  # 模型这一步什么都没给：日志里留着供排查，视图里不带，否则下一次调用会看到一条空的 assistant 消息
            msg = {"role": "assistant", "content": p.get("content") or ""}
            if calls:
                try:
                    msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                          "function": {"name": c["name"], "arguments": c["arguments"]}}
                                         for c in calls]
                except (KeyError, TypeError):
                    raise _corrupt(e, "tool_calls 缺 id、name 或 arguments") from None
            messages.append(msg)
            for c in calls:
                if c["id"] not in results:
                    raise _corrupt(e, f"工具调用 {c['id']} 没有结果")
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": TRIMMED if c["id"] in cleared else _tool_content(results[c["id"]])})
    return messages

