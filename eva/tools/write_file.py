"""write_file：把沙箱工作目录里的文件保存为租户的文件，得到 file_id。

保存后 read_file、run_python(file_ids) 都能按 file_id 引用它。同一租户内容相同的文件只有一份，
重复保存沿用原 file_id（existing=true）。文件由宿主从工作目录取出并校验，沙箱里碰不到存储。
"""
import json
import mimetypes
from typing import Callable

from eva.contracts.tooling import ToolContext
from eva.sandbox import Sandbox
from eva.storage import db
from eva.storage.objects import ObjectStore, Objects
from eva.tools.base import Tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1,
                 "description": "run_python 工作目录里的文件路径（相对路径，用 / 分隔），取自 run_python 返回的 files"},
    },
    "required": ["path"],
    "additionalProperties": False,
}

_DESCRIPTION = (
    "把 run_python 在工作目录里生成的文件保存下来，返回 file_id。"
    "保存后的文件才能被 read_file 读取、被后续 run_python 作为 file_ids 使用。"
    "文件名取路径的最后一段。内容和已保存的文件完全相同时不会新增，返回原来的 file_id，existing 为 true。"
)


def make_write_file(sandbox: Sandbox, store: ObjectStore, open_tx: Callable = db.transaction) -> Tool:
    def run(args: dict, ctx: ToolContext) -> str:
        path = args["path"]
        data = sandbox.read_artifact(ctx.tenant_id, ctx.session_id, path)
        filename = path.rsplit("/", 1)[-1]
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open_tx() as conn:
            row, existing = Objects(conn, ctx.tenant_id, store).save_original(filename, mime, data)
        return json.dumps({"file_id": row["file_id"], "filename": row["filename"], "bytes": row["bytes"],
                           "existing": existing}, ensure_ascii=False)

    return Tool("write_file", _DESCRIPTION, _PARAMETERS, run)
