"""query_company：按统一社会信用代码查一家公司的某个维度（工商登记、股东、对外投资、实际控制人、主要人员、董监高关联企业、失信被执行、行政处罚、裁判文书、招投标等），
数据来自 QCC（企查查）。维度在 tools.yaml 的 qcc.dimensions 里登记，加维度不改代码。

只收信用代码，不收名字：没经 resolve_entity 对齐、不在公司主数据里的公司不能查。
QCC 返回的是"当前状态"，本身不带日期，所以每条记录由我们记下取数时间，按会话编号（q1、q2…）存库，回答里的引用靠它核对。
同一家公司的同一维度在有效期内只向 QCC 付一次费：结果存公共缓存 qcc_cache（所有租户共享），命中时不出网、不记账，返回里写明当初的取数时间。
没匹配上的结果不进缓存。
每次出网按次记账：先预扣，拿到结果结算，失败释放。查不到（实体或人名没匹配上）是正常结果；返回缺记录列表是协议异常，抛错。
返回里服务方写的说明文字（摘要、提示）不转交给模型，只用来判断有没有被截断。
每次调用的结果（有记录、没有记录、没匹配上、出错）在会话里留痕到 qcc_queries，场景报告交付前核对必备维度有没有查过靠它。
"""
import hashlib
import json
import re
from datetime import timedelta
from typing import Callable, Optional

from eva.common import clock
from eva.contracts.errors import Code, EvaError
from eva.contracts.tooling import ToolContext
from eva.contracts.vocab import Concurrency, Purpose, QccOutcome
from eva.storage import db
from eva.storage.companies import Companies
from eva.storage.ledger import open_ledger
from eva.storage.qcc_cache import QccCache
from eva.storage.qcc_queries import QccQueries
from eva.storage.qcc_results import QccResults
from eva.tools.base import Tool
from eva.tools.config import QccConfig, QccDimension
from eva.tools.qcc import USCC, Qcc

_MODEL_LABEL = "qcc:"
_TOTAL = re.compile(r"共有\s*(\d+)\s*条")
_MAX_PERSON_CHARS = 64


def _description(cfg: QccConfig) -> str:
    dims = "；".join(f"{name}（{d.about}{'，需要 person' if d.person else ''}）" for name, d in cfg.dimensions.items())
    return (
        "按统一社会信用代码查一家公司的某个维度，数据来自企查查。公司必须先用 resolve_entity 对齐到信用代码，不接受公司名。"
        f"可查的维度：{dims}。"
        "返回的每条记录有编号 [q1]、[q2]…，写进回答时引用成" + "【q编号「摘抄原文」】" + "，摘抄取自该条记录，会被逐条核对。"
        "数据是取数时间点的状态，不是历史，也可能是几天前取的（返回里写明了取数时间），需要告诉用户数据时间；持股、任职以返回为准。名单里的股东、被投资企业只有名字没有信用代码，要继续往下穿透就对名字再 resolve_entity，"
        "对不上的（自然人、境外主体、简称）如实标成未对齐，不要猜。返回写明「只展示了前 N 条」时说明名单不全，不要当成全部。"
    )


def _parameters(cfg: QccConfig) -> dict:
    return {
        "type": "object",
        "properties": {
            "uscc": {"type": "string", "pattern": "^[0-9A-Z]{18}$", "description": "统一社会信用代码，取自 resolve_entity 的返回"},
            "dimension": {"type": "string", "enum": list(cfg.dimensions), "description": "要查的维度"},
            "person": {"type": "string", "minLength": 1, "maxLength": _MAX_PERSON_CHARS,
                       "description": "人名，只有标明需要 person 的维度才填，必须是这家公司的董监高"},
        },
        "required": ["uscc", "dimension"],
        "additionalProperties": False,
    }


def _value(v) -> str:
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, list):
        return "、".join(_value(x) for x in v if _value(x))
    if v is None:
        return ""
    return json.dumps(v, ensure_ascii=False)


def _line(record) -> str:
    """一条记录压成一行"字段：值；字段：值"，空值不显示。"""
    if not isinstance(record, dict):
        raise EvaError(Code.ENTITY_UNAVAILABLE, "企业数据源的记录不是对象", got=str(record)[:100])
    text = "；".join(f"{k}：{_value(v)}" for k, v in record.items() if _value(v))
    if not text:
        raise EvaError(Code.ENTITY_UNAVAILABLE, "企业数据源返回了空记录")
    return text


def _records(payload: dict, dim: QccDimension, no_record_text: str) -> Optional[list]:
    """记录列表；服务方明说没匹配上返回 None；说明文字里明说没有记录返回空列表；都不是则是协议异常。
    工商登记这类单条记录的维度，整个返回就是那一条。"""
    if "无匹配项" in payload:
        return None
    if dim.single:
        return [payload]
    rows = next((payload[k] for k in (dim.list_key, *dim.alt_list_keys) if isinstance(payload.get(k), list)), None)
    if rows is None and any(isinstance(v, str) and no_record_text in v for v in payload.values()):
        return []
    if rows is None:
        raise EvaError(Code.ENTITY_UNAVAILABLE, "企业数据源返回里没有预期的记录列表", expected=dim.list_key, keys=list(payload)[:10])
    return rows


def _truncated(payload: dict, shown: int) -> Optional[int]:
    """被截断时返回真实总数，服务方说截断了但没给总数返回 0，没截断返回 None。"""
    m = _TOTAL.search(str(payload.get("摘要") or ""))
    total = int(m.group(1)) if m else None
    if total is not None and total > shown:
        return total
    return 0 if payload.get("提示") else None


def make_query_company(cfg: QccConfig, qcc: Qcc, open_tx: Callable = db.transaction, open_ledger_fn: Callable = open_ledger) -> Tool:
    def run(args: dict, ctx: ToolContext) -> str:
        uscc, name = args["uscc"], args["dimension"]
        dim, person = cfg.dimensions.get(name), (args.get("person") or "").strip()
        if not USCC.match(uscc) or dim is None:
            raise EvaError(Code.TOOL_ARGS_INVALID, "uscc 要是 18 位信用代码，dimension 要在可查的维度里", tool="query_company")
        if dim.person != bool(person):
            raise EvaError(Code.TOOL_ARGS_INVALID, f"维度 {name} " + ("需要 person" if dim.person else "不接受 person"), tool="query_company")
        def note(outcome: QccOutcome) -> None:
            with open_tx() as conn:
                QccQueries(conn, ctx.tenant_id).add(ctx.session_id, uscc, name, outcome)

        with open_tx() as conn:
            company = Companies(conn, ctx.tenant_id).get(uscc)
            cached = QccCache(conn, ctx.tenant_id).get(uscc, name, person) if company else None
        if company is None:
            raise EvaError(Code.COMPANY_NOT_FOUND, "这家公司不在主数据里，先用 resolve_entity 对齐", uscc=uscc)
        label = f"{company['full_name']}（{uscc}）· {name}" + (f" · {person}" if person else "")
        if cached is not None and clock.now() - cached["fetched_at"] < timedelta(seconds=cfg.cache_ttl_seconds):
            rows, cut, fetched, how = cached["rows"], cached["cut"], cached["fetched_at"], "此前查过，未重新取数"
        else:
            try:
                payload = _fetch(dim, name, uscc, person, ctx)
                rows = _records(payload, dim, cfg.no_record_text)
            except EvaError:
                note(QccOutcome.FAILED)
                raise
            if rows is None:  # 没匹配上不缓存：人名是用户随口说的，不往公共层放没经数据源确认的文字
                note(QccOutcome.UNMATCHED)
                return f"{label}：没有匹配上" + ("，请核实人名是否是这家公司的董监高。" if dim.person else "。")
            cut = _truncated(payload, len(rows))
            with open_tx() as conn:
                fetched = QccCache(conn, ctx.tenant_id).put(uscc, name, person, rows, cut)
            how = "刚取数"
        if not rows:
            note(QccOutcome.NONE)
            return f"{label}：没有记录（取数时间 {fetched:%Y-%m-%d %H:%M}，{how}）。"
        lines = [_line(r) for r in rows]
        with open_tx() as conn:
            numbers = QccResults(conn, ctx.tenant_id).add(ctx.session_id, ctx.call_id, uscc, name, person or None, lines, fetched)
        note(QccOutcome.RECORDS)
        scope = "全部" if cut is None else f"只展示了前 {len(rows)} 条" + (f"，共 {cut} 条" if cut else "，名单不全")
        head = f"{label} · 取数时间 {fetched:%Y-%m-%d %H:%M}（{how}）· {len(rows)} 条（{scope}）。工商登记信息，取数时间点的状态。"
        return "\n".join([head] + [f"[q{n}] {line}" for n, line in zip(numbers, lines)])

    def _fetch(dim: QccDimension, name: str, uscc: str, person: str, ctx: ToolContext) -> dict:
        idem = "qcc:" + hashlib.sha1(ctx.call_id.encode("utf-8")).hexdigest()[:24]
        model = _MODEL_LABEL + dim.tool
        with open_ledger_fn(ctx.tenant_id) as led:
            hold_id = led.hold(ctx.session_id, idem, Purpose.TOOL_CALL.value, dim.price_cny, cfg.pricing_version, model=model)
        try:
            payload = qcc.call(dim.server, dim.tool, {"searchKey": uscc, **({"personName": person} if person else {})})
        except BaseException:
            with open_ledger_fn(ctx.tenant_id) as led:  # 没拿到结果就不收费
                led.release(hold_id, idem)
            raise
        with open_ledger_fn(ctx.tenant_id) as led:
            led.settle(hold_id, idem, dim.price_cny, cfg.pricing_version, model=model)
        return payload

    return Tool("query_company", _description(cfg), _parameters(cfg), run, concurrency=Concurrency.SHARED)
