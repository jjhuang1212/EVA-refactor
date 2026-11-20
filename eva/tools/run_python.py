"""run_python：在沙箱里运行模型写的 Python。会话内工作目录的文件保留，输入文件由宿主从对象存储取来放进去。

沙箱里碰不到存储和凭证。代码自己的失败（退出码非 0、traceback）是正常结果，原样返回；沙箱的故障抛错误码。
"""
import json
import ntpath
import re
from typing import Callable, Mapping

from eva.contracts.errors import Code, EvaError
from eva.contracts.tooling import ToolContext
from eva.sandbox import Sandbox
from eva.sandbox.config import SandboxConfig
from eva.storage import db
from eva.storage.objects import ObjectStore, Objects
from eva.tools.base import Tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "minLength": 1, "description": "要运行的完整 Python 脚本"},
        "file_ids": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True,
                     "description": "要放进工作目录的上传文件；返回的 inputs 里给出它们的路径"},
    },
    "required": ["code"],
    "additionalProperties": False,
}


def _description(cfg: SandboxConfig) -> str:
    return (
        "在隔离的沙箱里运行 Python 脚本，用来做统计、格式转换、编辑 docx/xlsx 等。"
        "没有网络。同一会话里工作目录（当前目录）的文件会保留，下次调用还能读到，可以先写脚本文件再导入；"
        "进程和变量不保留。用 print 输出结果。"
        f"单次运行不超过 {cfg.timeout_seconds:g} 秒。"
        f"本次新增或修改的文件会列在 files 里，类型只能是 {' '.join(cfg.allowed_extensions)}；类型或大小不合规的文件只会被拒收，列在 rejected_files 里，其余文件照常。"
        "exit_code 非 0 说明脚本出错，stderr 里有原因，改代码后可重试。"
    )


def _safe_name(filename: str) -> str:
    name = re.sub(r"[:\\\x00-\x1f]", "_", ntpath.basename(filename))  # ntpath 同时认 / 和 \
    if name in ("", ".", ".."):
        raise EvaError(Code.INVALID_VALUE, "上传文件的文件名不能作为目录里的文件名", filename=filename)
    return name


def make_run_python(sandbox: Sandbox, store: ObjectStore, cfg: SandboxConfig, open_tx: Callable = db.transaction,
                    skill_files: Callable[[], Mapping[str, bytes]] = dict) -> Tool:
    """skill_files 每次运行时调用，返回各 skill 的脚本（工作目录里的相对路径 -> 内容），放进去让模型 import 即可用。
    传函数而不是字典，skill 热加载后下一次运行就用新脚本。"""
    def run(args: dict, ctx: ToolContext) -> str:
        files = {}
        for file_id in args.get("file_ids", []):
            with open_tx() as conn:
                row, data = Objects(conn, ctx.tenant_id, store).read_original(file_id, cfg.max_input_bytes)
            files[f"inputs/{file_id}/{_safe_name(row['filename'])}"] = data
        inputs = list(files)
        res = sandbox.run(ctx.tenant_id, ctx.session_id, args["code"], {**skill_files(), **files}, ctx.cancel)
        return json.dumps({
            "exit_code": res.exit_code, "stdout": res.stdout, "stderr": res.stderr,
            "duration_seconds": round(res.duration_seconds, 2), "inputs": inputs,
            "files": [{"path": a.path, "bytes": a.bytes} for a in res.files],
            **({"rejected_files": list(res.rejected), "rejected_note": "这些文件类型或大小不合规，没有登记为产物；files 里的文件不受影响，不用重跑"}
               if res.rejected else {}),
        }, ensure_ascii=False)

    return Tool("run_python", _description(cfg), _PARAMETERS, run)
