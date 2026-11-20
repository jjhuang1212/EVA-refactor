"""new_topic：用户要换个话题、从头聊时调用。此前的对话不再进入模型视图，记住的偏好不受影响。

只是不再使用，日志里的原文还在，所以回复里不能说成"已删除"。重置事件由循环写（Tool.resets），
这里只给出回复；"新话题"指令走企微渠道时用的是同一段回复。
"""
from eva.contracts.tooling import ToolContext
from eva.tools.base import Tool

NEW_TOPIC = "new_topic"
NEW_TOPIC_REPLY = "好的，从新话题开始，之前的对话我不会再参考。已记住的偏好仍然有效。"

_DESCRIPTION = (
    "用户要换话题、从头开始、或让你不要再参考之前的聊天内容时调用，调用后此前的对话不再使用，已记住的偏好不受影响。"
    "只有用户在这一轮亲口提出才能调用。这只是不再参考，不是删除数据，不要对用户说成\"已删除\"。"
    "用户说\"清空记忆\"拿不准指聊天还是偏好时，先用 ask_user 问清楚。"
)


def make_new_topic() -> Tool:
    def run(args: dict, ctx: ToolContext) -> str:
        return NEW_TOPIC_REPLY

    return Tool(NEW_TOPIC, _DESCRIPTION, {"type": "object", "properties": {}, "additionalProperties": False}, run,
                resets=True)
