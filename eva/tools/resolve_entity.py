"""resolve_entity：把用户说的公司名对齐到工商登记的公司，返回统一社会信用代码（USCC）。

分工：简称展开成全称、错别字改正这类需要常识的事归模型（它的知识比任何别名表都全），本工具只做确定性的部分：
名称规范化、查本地（本租户确认过的称呼、公共主数据）、本地没有再问 QCC，并给每个候选附上判断依据（是否上市、名字上和查询词的关系）。
QCC 查到的公司写进公共主数据（只补缺）。查无此公司是空列表，数据源故障才抛错误码。每次出网按次记账：先预扣，拿到结果结算，失败释放。
"""
import hashlib
import json
from typing import Callable, List

from eva.contracts.errors import Code, EvaError
from eva.contracts.tooling import ToolContext
from eva.contracts.vocab import Concurrency, Purpose
from eva.storage import db
from eva.storage.companies import Companies, normalize_name
from eva.storage.ledger import open_ledger
from eva.tools.base import Tool
from eva.tools.config import QccConfig
from eva.tools.qcc import Outcome, Qcc

_MODEL_LABEL = "qcc:get_company_by_query"
_PROFILE = (("成立日期", "established"), ("法定代表人名称", "legal_representative"), ("状态", "status"))

_DESCRIPTION = (
    "把用户说的公司名对齐到工商登记的公司，返回候选的全称和统一社会信用代码。"
    "要查这家公司的股东、投资、任职，或把它写进报告之前先调用；访谈里顺口提到的公司不用对齐。\n"
    "调用前先用你的知识把名字改成最可能的工商登记全称再传：简称、品牌名、口语叫法要展开（如\"阿里\"想想是哪家主体），错别字要改正"
    "（拿不准是不是错别字，先用 search_hotwords 按读音找上市公司名）。全称能精确命中，简称只会得到一串字面相近的子公司。"
    "你不认识的公司（小公司、新公司、地方企业），先用 web_search 查它的全称再传，网页结果可能互相矛盾，只用来找名字，信用代码以本工具为准。\n"
    "返回的每个候选有：matched_by（tenant_alias 是本用户确认过的称呼，full_name、short_name、alias 是本地主数据，qcc 是刚从企业数据源查到的）；"
    "sec_code（有值说明是A股上市公司，出名的公司多半有）；name_match（候选名和你传的名字的关系：exact 完全相同、starts_with 以它开头、"
    "contains 包含它、none 字面上看不出关系，企业数据源是按简称或品牌名匹配的，要凭你的了解判断）。\n"
    "怎么选：只有一个候选，或有一个候选明显就是用户说的那家（出名、上市、全称吻合），就直接用，并在回复里写明所选公司的全称和信用代码，用户可以纠正。"
    "用户说的是集团或品牌（如\"阿里\"\"字节\"），而候选是旗下多家不同的子公司，或几家公司你和用户都无法分出是哪家时，不要替用户挑，"
    "用 ask_user 问一次，把候选的全称、法定代表人、成立日期列给用户，一次只问一个公司。选定后，如果这个叫法以后还会用到，用 confirm_alias 记下。\n"
    "matched_by=alias 且只有一个候选时，通常是集团或品牌的常用主体（如\"腾讯\"对应深圳市腾讯计算机系统有限公司），直接用，回复里点明所选主体并提一句\"旗下还有其他主体，需要请说全称\"。\n"
    "没有候选或候选都是 name_match=none 时，说明企业数据源没有匹配：可能名字有误，也可能是境外注册的主体（如腾讯控股、阿里巴巴集团控股，"
    "没有统一社会信用代码）。换全称再试，或用 web_search 确认；仍然没有，就告诉用户没有查到这家公司的境内登记信息（境外注册的主体直接说明它注册在境外），改查它的境内主要运营主体，不要编造信用代码，也不要拿不相关的候选凑数。对用户不要提数据来源的名称。"
    "全称和信用代码一律取自本工具的返回，不要凭记忆写。"
)
_PARAMETERS = {
    "type": "object",
    "properties": {"name": {"type": "string", "minLength": 1, "description": "公司名，最好是你判断出的工商登记全称；只放名字，不要放整句话"}},
    "required": ["name"],
    "additionalProperties": False,
}


def _profile(raw: dict) -> dict:
    """候选的辅助信息（成立日期、法定代表人、经营状态），帮模型区分同名公司。只取固定字段，QCC 返回里的其他文字（含给模型的"摘要"指令）不转交。"""
    return {en: raw[cn] for cn, en in _PROFILE if isinstance(raw.get(cn), (str, list)) and raw[cn]}


def name_match(query: str, name: str) -> str:
    q, n = normalize_name(query), normalize_name(name)
    return "exact" if q == n else "starts_with" if n.startswith(q) else "contains" if q in n else "none"


def make_resolve_entity(cfg: QccConfig, qcc: Qcc, open_tx: Callable = db.transaction,
                        open_ledger_fn: Callable = open_ledger) -> Tool:
    def local(name: str, tenant_id: str) -> list:
        with open_tx() as conn:
            return Companies(conn, tenant_id).resolve(name)

    def remote(name: str, ctx: ToolContext) -> Outcome:
        idem = "entity:" + hashlib.sha1(ctx.call_id.encode("utf-8")).hexdigest()[:24]
        with open_ledger_fn(ctx.tenant_id) as led:
            hold_id = led.hold(ctx.session_id, idem, Purpose.TOOL_CALL.value, cfg.search_price_cny, cfg.pricing_version,
                               model=_MODEL_LABEL)
        try:
            outcome = qcc.search(name)
        except BaseException:
            with open_ledger_fn(ctx.tenant_id) as led:  # 没拿到结果就不收费
                led.release(hold_id, idem)
            raise
        with open_ledger_fn(ctx.tenant_id) as led:
            led.settle(hold_id, idem, cfg.search_price_cny, cfg.pricing_version, model=_MODEL_LABEL)
        return outcome

    def from_qcc(outcome: Outcome, ctx: ToolContext) -> List[dict]:
        """候选写进公共主数据，再回读一遍：主数据里已有的行带着简称和上市信息（旧库迁移来的上市公司）。"""
        out = []
        with open_tx() as conn:
            repo = Companies(conn, ctx.tenant_id)
            for hit in outcome.hits:
                repo.record_qcc(hit.uscc, hit.name, hit.raw)
                row = repo.get(hit.uscc)
                out.append({"uscc": hit.uscc, "full_name": hit.name, "short_name": row["short_name"], "matched_by": "qcc",
                            "sec_code": row["sec_code"], **_profile(hit.raw)})
        return out

    def run(args: dict, ctx: ToolContext) -> str:
        name = args["name"].strip()
        if not name or len(name) > cfg.max_name_chars:
            raise EvaError(Code.TOOL_ARGS_INVALID, f"公司名不能为空，也不能超过 {cfg.max_name_chars} 个字符", tool="resolve_entity")
        found, result = local(name, ctx.tenant_id), {"name": name}
        if found:
            result["matches"] = [{**m, "name_match": name_match(name, m["full_name"])} for m in found]
            return json.dumps(result, ensure_ascii=False)
        outcome = remote(normalize_name(name), ctx)
        result["matches"] = [{**m, "name_match": name_match(name, m["full_name"])} for m in from_qcc(outcome, ctx)]
        result["unique"] = outcome.unique
        if not result["matches"]:
            result["note"] = "没有查到匹配的境内登记信息：名字可能有误，也可能是境外注册的主体（没有统一社会信用代码）。换全称再试，或用 web_search 确认。"
        elif all(m["name_match"] == "none" for m in result["matches"]):
            result["note"] = "所有候选的名字都和你传的对不上，是按简称或品牌名匹配的，很可能不是你要找的公司（也可能你要找的是境外注册的主体）；拿不准就不要选。"
        return json.dumps(result, ensure_ascii=False)

    return Tool("resolve_entity", _DESCRIPTION, _PARAMETERS, run, concurrency=Concurrency.SHARED)
