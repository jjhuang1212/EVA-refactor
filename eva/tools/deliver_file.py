"""deliver_file：登记一个已保存的文件，本轮回答结束时随回复一起交给用户。

工具只负责登记，真正怎么发（上传、条数、大小上限）是渠道的事。登记存在 Outbox 里，由 Conversation 在 turn 结束后取走。
"""
import json
import re
from typing import Callable, Dict, Optional, Tuple

from eva.common.manifest import read_manifest
from eva.contracts.channel import ReplyFile
from eva.contracts.errors import Code, EvaError
from eva.contracts.tooling import ToolContext
from eva.storage import db
from eva.storage.deliveries import Deliveries
from eva.storage.objects import ObjectStore, Objects
from eva.tools.base import Tool
from eva.tools.config import DeliverFileConfig

_PARAMETERS = {
    "type": "object",
    "properties": {
        "file_id": {"type": "string", "minLength": 1,
                    "description": "要交给用户的文件，取自 write_file 返回的 file_id（用户上传的文件也可以）"},
        "inline_if_short": {"type": "boolean",
                            "description": "只用于 UTF-8 文本文件（如 md）：内容不长时直接以文字发给用户，太长才发文件。默认 false"},
        "immediately": {"type": "boolean",
                        "description": "true 表示现在就发给用户，不等这一轮回答结束；用在中间产物（如原始转录），让用户先看到。默认 false"},
    },
    "required": ["file_id"],
    "additionalProperties": False,
}

_DESCRIPTION = (
    "把一个已保存的文件交给用户，例如最终的 PDF 报告。文件会在你这一轮回答结束时随回复发出，"
    "所以回答里要说明这是什么文件，不要写成已经发出；immediately=true 的文件当场发出，结果里 sent 为 true。同一个文件登记多次只发一份。"
    "先用 run_python 生成、write_file 保存，再用这里登记。"
)


# 登记时同名不同内容会改成"名字(2).扩展名"，那是内部为了引用唯一，用户看到的文件名不该带序号
_RENAME_SUFFIX = re.compile(r"\(\d+\)(?=\.[^.]*$)")


class Outbox:
    """一个租户的 turn 内待交付的文件。同一租户一次只跑一个 turn，所以不需要锁。"""

    def __init__(self):
        self._files: Dict[str, ReplyFile] = {}
        self._sink: Optional[Callable[[ReplyFile], bool]] = None

    def set_sink(self, sink: Optional[Callable[[ReplyFile], bool]]) -> None:
        """渠道给的即时投递回调，只在一个 turn 内有效；返回 False（渠道此刻不宜先发）或没有回调时，immediately 退化成随回复一起发。"""
        self._sink = sink

    def add(self, file_id: str, file: ReplyFile, immediately: bool = False) -> bool:
        """返回文件是否已经当场交给渠道发出。当场发的不再进 turn 结束时的取走清单。"""
        if immediately and self._sink is not None and self._sink(file):
            self._files.pop(file_id, None)
            return True
        self._files[file_id] = file
        return False

    def notify(self, text: str) -> bool:
        """当场发一段短文字给用户；渠道此刻不宜先发或没有即时回调就不发（不排进最终回复）。文字当作够短的文本文件交给渠道，渠道会直接发成文字。"""
        return self._sink is not None and bool(self._sink(ReplyFile("通知.md", "text/markdown", text.encode("utf-8"), True)))

    def clear(self) -> None:
        self._files.clear()

    def take(self) -> Tuple[ReplyFile, ...]:
        files = tuple(self._files.values())
        self._files.clear()
        return files


def make_deliver_file(store: ObjectStore, outbox: Outbox, cfg: DeliverFileConfig, open_tx: Callable = db.transaction) -> Tool:
    def run(args: dict, ctx: ToolContext) -> str:
        with open_tx() as conn:
            row, data = Objects(conn, ctx.tenant_id, store).read_original(args["file_id"], cfg.max_file_bytes)
        inline = args.get("inline_if_short", False)
        if inline:
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as e:
                raise EvaError(Code.INVALID_VALUE, "inline_if_short 只能用于 UTF-8 文本文件", filename=row["filename"]) from e
        scenario = read_manifest(data)[1]  # 清单有问题的文件由交付前的核对拦下，这里只取场景声明
        with open_tx() as conn:
            Deliveries(conn, ctx.tenant_id).add(row["file_id"], ctx.session_id, scenario and scenario["name"], scenario and scenario["uscc"])
        shown = _RENAME_SUFFIX.sub("", row["filename"])
        sent = outbox.add(row["file_id"], ReplyFile(shown, row["mime"], data, inline, row["file_id"]), args.get("immediately", False))
        return json.dumps({"file_id": row["file_id"], "filename": shown, "bytes": row["bytes"],
                           "sent": sent, "queued": not sent}, ensure_ascii=False)

    return Tool("deliver_file", _DESCRIPTION, _PARAMETERS, run)
