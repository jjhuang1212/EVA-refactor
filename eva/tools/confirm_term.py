"""confirm_term:记下用户确认过的写法(公司名、人名、术语的正确写法,以及常被听成什么),之后转写纠错优先用它。

只在用户明确答复了"这个词是不是 X"之后调用,模型自己推断出来的不能记。写入只属于当前租户,不进公共热词表。
"""
import json
from typing import Callable

from eva.contracts.errors import Code, EvaError
from eva.contracts.tooling import ToolContext
from eva.contracts.vocab import TermKind
from eva.storage import db
from eva.storage.tenant_terms import TenantTerms
from eva.tools.base import Tool
from eva.tools.config import ConfirmTermConfig

_DESCRIPTION = (
    "记下用户确认过的写法,以后转写纠错会优先用它。只有用户明确答复了你的确认问题(比如你问\"访谈里的宁得时代是宁德时代吗\",用户答是)才能调用;"
    "你自己判断的、没问过用户的不要记。term 是正确写法,heard_as 是它常被听成的错误写法(可选),kind 是公司、人名或术语。"
    "同一个 term 再次确认会覆盖原来的;remove=true 删除一条记错的。"
)
_PARAMETERS = {
    "type": "object",
    "properties": {
        "term": {"type": "string", "minLength": 1, "description": "正确写法"},
        "heard_as": {"type": "string", "minLength": 1, "description": "常被听成的错误写法"},
        "kind": {"type": "string", "enum": [k.value for k in TermKind], "description": "company 公司名、person 人名、term 术语"},
        "remove": {"type": "boolean", "description": "为 true 时删除这条写法,此时不需要 kind"},
    },
    "required": ["term"],
    "additionalProperties": False,
}


def make_confirm_term(cfg: ConfirmTermConfig, open_tx: Callable = db.transaction) -> Tool:
    def run(args: dict, ctx: ToolContext) -> str:
        with open_tx() as conn:
            terms = TenantTerms(conn, ctx.tenant_id)
            if args.get("remove"):
                return json.dumps({"term": args["term"], "removed": terms.remove(args["term"])}, ensure_ascii=False)
            if "kind" not in args:
                raise EvaError(Code.TOOL_ARGS_INVALID, "记下写法需要 kind", tool="confirm_term")
            created = terms.confirm(args["term"], args["kind"], args.get("heard_as"), ctx.session_id,
                                    cfg.max_terms_per_tenant, cfg.max_term_chars)
        return json.dumps({"term": args["term"], "saved": True, "new": created}, ensure_ascii=False)

    return Tool("confirm_term", _DESCRIPTION, _PARAMETERS, run)
