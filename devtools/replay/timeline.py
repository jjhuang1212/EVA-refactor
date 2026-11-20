"""把一个会话还原成时间线：transcript 事件、账本里的每次模型尝试、对象存储里的调用记录合在一起。

只产出结构化数据，怎么显示由渲染端决定（终端、将来的网页）。只经存储层的读接口取数，租户过滤照常生效；
别的租户的会话和不存在的会话一样报 SESSION_NOT_FOUND。
"""
import re
from collections import Counter
from decimal import Decimal
from typing import Optional

from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import EventType, Purpose
from eva.storage.ledger import Ledger
from eva.storage.objects import Objects
from eva.storage.sessions import Sessions
from eva.storage.transcript import Transcript

# 模型调用层的幂等键是"调用号:尝试号"；ASR 和工具自己计费（search:、entity:、qcc: 加哈希）不经它，没有调用记录
_CLIENT_IDEM = re.compile(r"[^:]+:\d{1,2}")


def build(conn, store, tenant_id: str, session_id: str, with_blobs: bool = False) -> dict:
    """返回 {"session_id", "tenant_id", "items", "totals", "blobs"}。

    items 按时间排序，每项 {"kind", "at", ...}：transcript 事件原样带 payload，模型回复事件挂上它对应的那次调用；
    其余模型调用（工具内调用、压缩、失败的尝试、没写进日志的调用）是独立的 "model_call" 项。
    """
    Sessions(conn, tenant_id).require(session_id)
    objects = Objects(conn, tenant_id, store)
    attempts = _attempts(Ledger(conn, tenant_id).session_events(session_id), objects, session_id)
    pending = [a for a in attempts if a["purpose"] == Purpose.TURN.value and a["end"] == "settle"]
    items, used, reasoning = [], set(), None
    for e in Transcript(conn, tenant_id).read(session_id):
        kind = e["event_type"]
        if kind == EventType.REASONING.value:
            reasoning = e["payload"]  # 并进紧随其后的模型回复，不单独占一行
            continue
        item = {"kind": kind, "seq": e["seq"], "at": e["created_at"], "payload": e["payload"]}
        if kind == EventType.ASSISTANT_MESSAGE.value:
            ready = [a for a in pending if a["hold_id"] not in used and a["end_at"] <= e["created_at"]]
            item["call"] = ready[-1] if ready else None
            item["transcript_reasoning"], reasoning = reasoning, None
            if ready:
                used.add(ready[-1]["hold_id"])
        items.append(item)
    items += [{"kind": "model_call", "at": a["at"], "call": a} for a in attempts if a["hold_id"] not in used]
    items.sort(key=lambda i: i["at"])  # 稳定排序：同一时刻 transcript 事件在前
    blobs = _blobs(objects, attempts) if with_blobs else {}
    return {"session_id": session_id, "tenant_id": tenant_id, "items": items, "totals": _totals(items, attempts),
            "blobs": blobs}


def _attempts(rows: list, objects: Objects, session_id: str) -> list:
    """账本里每次预扣是一次尝试：hold 之后跟 settle 或 release，都没有就是悬空。附上它的调用记录，没有记录为 None。"""
    by_hold = {}
    for r in rows:
        a = by_hold.setdefault(r["hold_id"], {"hold_id": r["hold_id"], "end": None})
        if r["kind"] == "hold":
            a.update(idem_key=r["idem_key"], purpose=r["purpose"], model=r["model"], at=r["created_at"])
        else:
            a.update(end=r["kind"], end_at=r["created_at"], cost_cny=r["amount_cny"] if r["kind"] == "settle" else None,
                     tokens_in=r["tokens_in"], tokens_out=r["tokens_out"])
    out = sorted(by_hold.values(), key=lambda a: a["at"])
    for a in out:
        a["end_at"] = a.get("end_at")
        a["expects_record"] = a["purpose"] != Purpose.ASR.value and bool(_CLIENT_IDEM.fullmatch(a["idem_key"]))
        a["record"] = _missing_ok(objects.read_call_record, session_id, a["idem_key"]) if a["expects_record"] else None
    return out


def _missing_ok(fn, *args) -> Optional[object]:
    """只有"不存在"算缺失；存储连不上等其他错误照常抛出。"""
    try:
        return fn(*args)
    except EvaError as e:
        if e.code is Code.FILE_NOT_FOUND:
            return None
        raise


def _blobs(objects: Objects, attempts: list) -> dict:
    shas = {r[k] for a in attempts if (r := a["record"]) for k in ("system_sha", "tools_sha") if r.get(k)}
    return {sha: _missing_ok(objects.read_blob, sha) for sha in sorted(shas)}


def _totals(items: list, attempts: list) -> dict:
    settled = [a for a in attempts if a["end"] == "settle"]
    events = [i for i in items if "payload" in i]
    return {
        "turns": sum(i["kind"] == EventType.TURN_START.value for i in events),
        "steps": sum(i["kind"] == EventType.ASSISTANT_MESSAGE.value for i in events),
        "cost_cny": str(sum((a["cost_cny"] for a in settled), Decimal(0))),
        "tokens_in": sum(a["tokens_in"] or 0 for a in settled),
        "tokens_out": sum(a["tokens_out"] or 0 for a in settled),
        "calls": dict(Counter(a["purpose"] for a in settled)),
        "failed_attempts": sum(a["end"] == "release" for a in attempts),
        "open_holds": sum(a["end"] is None for a in attempts),
        "missing_records": sum(a["expects_record"] and a["record"] is None for a in attempts),
        "tools": dict(Counter(i["payload"].get("name") for i in events if i["kind"] == EventType.TOOL_CALL.value)),
        "errors": sum(i["kind"] == EventType.ERROR.value for i in events),
    }
