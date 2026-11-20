"""export_transcript：把一份录音的转写稿存成 json 文件，之后可以作为 run_python 的 file_ids 在沙箱里处理（如转写纠错）。

导出 read_file 最近读的那一份转写；还没读过才转写。存下来的文件是转写稿的快照：块序号、文字、时间段锚点，和 read_file 里看到的一致。
"""
import json
import posixpath
from typing import Callable

from eva.contracts.document import Parsed
from eva.contracts.errors import Code, EvaError
from eva.contracts.tooling import ToolContext
from eva.storage import db
from eva.storage.objects import ObjectStore, Objects
from eva.tools.audio import Audio, load_transcript
from eva.tools.base import Tool

_DESCRIPTION = (
    "把一份录音的转写稿存成 json 文件并返回 file_id。之后把这个 file_id 放进 run_python 的 file_ids，"
    "就能在沙箱里按块处理它（比如做转写纠错，见 asr-correction skill）。"
    "文件内容是 {\"source\": 录音文件名, \"blocks\": [{\"ord\": 块号, \"text\": 文字, \"anchor\": 时间段}, ...]}。"
    "录音里没有说话内容时不生成文件，blocks 为 0。"
)
_PARAMETERS = {
    "type": "object",
    "properties": {"file_id": {"type": "string", "minLength": 1, "description": "录音文件的标识"}},
    "required": ["file_id"],
    "additionalProperties": False,
}


def make_export_transcript(store: ObjectStore, audio: Audio, open_tx: Callable = db.transaction) -> Tool:
    def run(args: dict, ctx: ToolContext) -> str:
        with open_tx() as conn:
            objects = Objects(conn, ctx.tenant_id, store)
            row = objects.describe(args["file_id"])
            latest = objects.read_latest_parsed(args["file_id"])
        filename = row["filename"]
        if posixpath.splitext(filename)[1].lower() not in audio.extensions:
            raise EvaError(Code.FILE_UNSUPPORTED, "不是录音文件，没有转写稿可导出", reason="type", filename=filename)
        if latest is not None:
            # 导出的必须是 read_file 刚读的那一份：换了人数重转写会得到另一份，块号和文字都可能对不上
            parsed = Parsed.from_dict(json.loads(latest))
        else:
            row, parsed = load_transcript(audio, store, open_tx, ctx.tenant_id, ctx.session_id, args["file_id"], ctx.cancel)
        if not parsed.blocks:
            return json.dumps({"blocks": 0, "warnings": list(parsed.warnings)}, ensure_ascii=False)
        body = {"source": row["filename"], "blocks": [{"ord": b.ord, "text": b.text, "anchor": b.anchor} for b in parsed.blocks]}
        name = f"{posixpath.splitext(row['filename'])[0]}_转写稿.json"
        with open_tx() as conn:
            saved, existing = Objects(conn, ctx.tenant_id, store).save_original(
                name, "application/json", json.dumps(body, ensure_ascii=False).encode("utf-8"))
        return json.dumps({"file_id": saved["file_id"], "filename": saved["filename"], "blocks": len(parsed.blocks),
                           "existing": existing, "warnings": list(parsed.warnings)}, ensure_ascii=False)

    return Tool("export_transcript", _DESCRIPTION, _PARAMETERS, run)
