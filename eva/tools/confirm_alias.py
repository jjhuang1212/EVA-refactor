"""confirm_alias:记下用户确认过的称呼对应哪家公司,下次 resolve_entity 直接命中。

只在用户明确说了"某某就是某某公司"、或明确纠正了你选错的公司之后调用;模型自己判断的不记。只属于当前租户,不进公共层。
"""
import json
from typing import Callable

from eva.contracts.tooling import ToolContext
from eva.storage import db
from eva.storage.companies import Companies
from eva.tools.base import Tool

_DESCRIPTION = (
    "记下用户确认过的称呼:用户说的某个叫法就是某家公司。只有用户明确说了\"X 就是 Y 公司\",或纠正了你对齐错的公司,才能调用;"
    "你自己判断的不要记。alias 是用户的叫法,uscc 取自 resolve_entity 的返回(公司必须已经被 resolve_entity 查到过)。"
    "同一个叫法再次确认会覆盖原来的。"
)
_PARAMETERS = {
    "type": "object",
    "properties": {"alias": {"type": "string", "minLength": 1, "description": "用户的叫法"},
                   "uscc": {"type": "string", "minLength": 18, "maxLength": 18, "description": "公司的统一社会信用代码"}},
    "required": ["alias", "uscc"],
    "additionalProperties": False,
}


def make_confirm_alias(open_tx: Callable = db.transaction) -> Tool:
    def run(args: dict, ctx: ToolContext) -> str:
        with open_tx() as conn:
            Companies(conn, ctx.tenant_id).confirm_alias(args["alias"].strip(), args["uscc"], ctx.session_id)
        return json.dumps({"alias": args["alias"].strip(), "uscc": args["uscc"], "saved": True}, ensure_ascii=False)

    return Tool("confirm_alias", _DESCRIPTION, _PARAMETERS, run)
