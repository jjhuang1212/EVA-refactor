"""load_skill：读一个 skill 的正文，或正文里提到的参考文件。系统提示里只有 skill 的名字和一行描述，要用时先读正文，
再按正文的指引读用得上的参考文件。

正文和参考文件都整段返回，不走溢出（skills.yaml 的 max_body_bytes、max_reference_bytes 管篇幅）。skill 的脚本不在这里返回：
它们每次运行 run_python 时已经放在工作目录的 skills/<名字>/ 下，这里只列出路径。
"""
from eva.contracts.tooling import ToolContext
from eva.skills.loader import Skills, render
from eva.tools.base import Tool

NAME = "load_skill"
_PARAMETERS = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "description": "skill 名，取自系统提示里的 skill 清单"},
        "file": {"type": "string", "minLength": 1, "description": "参考文件名，取自正文后附的参考文件清单；不填则读正文"},
    },
    "required": ["name"],
    "additionalProperties": False,
}

_DESCRIPTION = (
    "读取一个 skill 的正文。skill 写的是某类事情该怎么判断、怎么做，需要时先读，再照它做。"
    "正文后会列出该 skill 的参考文件，用到哪个就带 file 再读哪个。"
    "它的脚本已放在 run_python 的工作目录 skills/<名字>/ 下，可以直接运行或 import。"
)


def make_load_skill(skills: Skills) -> Tool:
    def run(args: dict, ctx: ToolContext) -> str:
        if "file" in args:
            return skills.reference(args["name"], args["file"])
        return render(skills.get(args["name"]))

    return Tool(NAME, _DESCRIPTION, _PARAMETERS, run, spills=False)
