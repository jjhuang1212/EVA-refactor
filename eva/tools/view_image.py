"""view_image：让视觉模型看一张用户上传的图片，按模型提的问题回答。

主模型不收图片，图片只在这里出现，上下文里始终是文字。视觉模型是 model.yaml 的 vision 角色，只用它，不切备选，
走统一调用层记账。长边超过配置的像素数先等比缩小，免得撞厂商对单张图的上限。回答是模型生成的"识别结果"，不是图片里的原文，返回里明说，也不给它编 [b] 块号，不能当引用出处。
文字类的图先用 read_file 的 OCR（原文，可核对）；OCR 没读出来或读出乱码、照片、图表、流程图再用这个。
文档（docx、pdf 等）里内嵌的图：read_file 读到的"看图识别"块是模型的一次概括，要针对原图追问就传 block（该块的块号）。
"""
import json
import posixpath
from typing import Callable

from eva.contracts.errors import Code, EvaError
from eva.contracts.tooling import ToolContext
from eva.contracts.vocab import Concurrency
from eva.model.client import ModelClient
from eva.storage import db
from eva.storage.objects import ObjectStore, Objects
from eva.tools.base import Tool
from eva.contracts.document import Parsed
from eva.tools.config import ViewImageConfig
from eva.tools.documents import Documents
from eva.tools.vision import fit, look

_FORMATS = {".png": ("image/png", b"\x89PNG\r\n\x1a\n"), ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
            ".jpeg": ("image/jpeg", b"\xff\xd8\xff")}
_NOTICE = "视觉模型的识别结果，不是图片里的原文，不能当引用出处；数字、金额、名称要请用户核对。"

_DESCRIPTION = (
    "让视觉模型看一张用户上传的图片并回答你的问题，用于照片、图表、流程图、手写，以及 read_file 的 OCR 没读出文字或读出乱码的图片（png、jpg）。"
    "文字为主的图（扫描件、截图）先用 read_file 读 OCR，那是原文，可以引用。"
    "文档里内嵌的图用 read_file 返回的看图识别块的块号作 block 追问，file_id 填文档的。"
    "question 要具体，如\"这张股权结构图里谁持股谁、各多少\"，不要问\"这是什么\"。"
    "返回的是模型生成的识别结果，不是原文，不能写成【b块号「摘抄」】引用；报告里用到时说明来自看图，数字和名称请用户核对。"
)
_PARAMETERS = {
    "type": "object",
    "properties": {
        "file_id": {"type": "string", "minLength": 1, "description": "上传图片的标识"},
        "question": {"type": "string", "minLength": 1, "description": "要从图里知道什么，越具体越好"},
        "block": {"type": "integer", "minimum": 0, "description": "file_id 是文档时，要看的内嵌图所在的块号（read_file 里的 [b块号]）；上传的图片不填"},
    },
    "required": ["file_id", "question"],
    "additionalProperties": False,
}


def make_view_image(store: ObjectStore, cfg: ViewImageConfig, model: ModelClient, documents: Documents,
                    open_tx: Callable = db.transaction) -> Tool:
    def embedded(objects: Objects, file_id: str, block: int) -> tuple:
        """文档里内嵌的图：（来源名, png）。块号必须是这份文档当前解析结果里登记过的图。"""
        row = objects.describe(file_id)
        raw = objects.read_parsed(file_id, documents.version)
        if raw is None:
            raise EvaError(Code.TOOL_ARGS_INVALID, "这份文档还没解析过，先用 read_file 读一遍再追问", tool="view_image",
                           reason="not_parsed", file_id=file_id)
        sha = Parsed.from_dict(json.loads(raw)).images.get(block)
        if sha is None:
            raise EvaError(Code.TOOL_ARGS_INVALID, "这个块号不是内嵌图，block 要填 read_file 里看图识别块的块号", tool="view_image",
                           reason="no_such_image", file_id=file_id, block=block)
        return f"{row['filename']} 的块 b{block}", objects.read_vision_image(sha)

    def run(args: dict, ctx: ToolContext) -> str:
        question = (args.get("question") or "").strip()
        if not question or len(question) > cfg.max_question_chars:
            raise EvaError(Code.TOOL_ARGS_INVALID, f"question 不能为空，也不能超过 {cfg.max_question_chars} 个字符", tool="view_image")
        block = args.get("block")
        if block is not None and (isinstance(block, bool) or not isinstance(block, int) or block < 0):
            raise EvaError(Code.TOOL_ARGS_INVALID, "block 必须是非负整数", tool="view_image")
        with open_tx() as conn:
            objects = Objects(conn, ctx.tenant_id, store)
            if block is not None:
                name, data = embedded(objects, args["file_id"], block)
                mime = "image/png"
            else:
                name = objects.describe(args["file_id"])["filename"]
                kind = _FORMATS.get(posixpath.splitext(name)[1].lower())
                if kind is None:
                    raise EvaError(Code.FILE_UNSUPPORTED, "只能看 png、jpg 图片；文档里的内嵌图要传 block", reason="type",
                                   file_id=args["file_id"], name=name)
                _, data = objects.read_original(args["file_id"], cfg.max_file_bytes)
                mime, magic = kind
                if not data.startswith(magic):
                    raise EvaError(Code.FILE_UNSUPPORTED, "文件内容不是它的扩展名所说的图片", reason="content", name=name)
        data, mime = fit(data, mime, cfg.max_edge)
        result = look(model, ctx.session_id, mime, data, question, cfg.max_output_tokens)
        text = (result.content or "").strip()
        if not text:
            raise EvaError(Code.MODEL_UNAVAILABLE, "视觉模型没有返回内容", model=result.model, finish_reason=result.finish_reason)
        out = {"source": name, "kind": "vision", "model": result.model, "notice": _NOTICE, "content": text}
        if result.finish_reason == "length":
            out["truncated"] = True
        return json.dumps(out, ensure_ascii=False)

    return Tool("view_image", _DESCRIPTION, _PARAMETERS, run, Concurrency.SHARED)
