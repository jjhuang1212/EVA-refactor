"""向用户提问并停放：本 turn 结束，用户的下一条输入就是这次调用的结果。"""
from eva.tools.base import Tool

ask_user = Tool(
    name="ask_user",
    description=(
        "信息不足且无法自行判断时，向用户提一个问题并等待回答。一次只问一个问题。"
        "答案是从几个明确选项里挑（如从几份历史报告里选）时，用 options 列出，渠道会渲染成菜单或编号文字；"
        "选项是用户看得懂的一句话，不要自己加序号。用户的回答可能是序号、\"序号. 选项文字\"，也可能是他自己的话，"
        "序号按 options 的顺序（从 1 起）对回你给的选项。用户可能一次选几个（\"1,3\"、\"1 和 3\"、\"都要\"），"
        "按选中的每一项分别处理；点菜单一次只能选一项，要多选的用户会直接打字。"),
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "minLength": 1, "description": "要问用户的问题"},
            "options": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 2, "uniqueItems": True,
                        "description": "可选。让用户从中挑一个的选项"},
        },
        "required": ["question"],
        "additionalProperties": False,
    },
    run=None,
    parks=True,
)
