"""list_files：列出本租户以前交付过的文件（报告、转写稿等），让用户能找回旧文件。

只列交付记录里有的可用文件，来源是 deliver_file 登记的交付，不是所有上传。拿到 file_id 后用 deliver_file 重新交付。
"""
import json
from typing import Callable

from eva.contracts.tooling import ToolContext
from eva.contracts.vocab import Concurrency
from eva.storage import db
from eva.storage.deliveries import Deliveries
from eva.tools.base import Tool
from eva.tools.config import ListFilesConfig

_PARAMETERS = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "description": "文件名里包含的字（如公司名、报告类型），不填就列最近的"},
        "uscc": {"type": "string", "pattern": "^[0-9A-Z]{18}$", "description": "统一社会信用代码，只列这家公司的报告"},
    },
    "additionalProperties": False,
}

_DESCRIPTION = (
    "列出以前交付给用户的文件（尽调报告、穿透报告、转写稿等），按交付时间从新到旧。"
    "用户提起以前的报告或文件、本会话里找不到 file_id 时用它。结果里有 file_id，再用 deliver_file 重新交付。"
    "没有匹配时返回空列表，说明确实没有交付过，不是出错。"
)


def make_list_files(cfg: ListFilesConfig, open_tx: Callable = db.transaction) -> Tool:
    def run(args: dict, ctx: ToolContext) -> str:
        with open_tx() as conn:
            rows = Deliveries(conn, ctx.tenant_id).recent(cfg.max_results, args.get("name"), args.get("uscc"))
        return json.dumps([{**r, "delivered_at": f"{r['delivered_at']:%Y-%m-%d %H:%M}"} for r in rows], ensure_ascii=False)

    return Tool("list_files", _DESCRIPTION, _PARAMETERS, run, Concurrency.SHARED)
