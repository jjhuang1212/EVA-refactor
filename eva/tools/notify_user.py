"""notify_user：长任务开始时先告诉用户要做什么，别让用户干等。

只负责交给 Outbox，渠道决定发不发、占不占配额（企微里占一条）。渠道当时不方便先发（配额要留给最终回复）
或没有即时投递回调时不发，返回 sent=false，不影响后面的流程。
"""
import json

from eva.contracts.tooling import ToolContext
from eva.tools.base import Tool
from eva.tools.deliver_file import Outbox

_PARAMETERS = {
    "type": "object",
    "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 300,
                            "description": "给用户看的一两句话：收到了什么、接下来做什么、预计交付哪些文件"}},
    "required": ["text"],
    "additionalProperties": False,
}

_DESCRIPTION = (
    "任务要花几分钟以上时，开始前先用它告诉用户你收到了什么、会交付哪几份文件。每个任务最多一次，不要拿它汇报进度或代替最终回答；"
    "最终回答仍要写。"
)


def make_notify_user(outbox: Outbox) -> Tool:
    def run(args: dict, ctx: ToolContext) -> str:
        return json.dumps({"sent": outbox.notify(args["text"].strip())})

    return Tool("notify_user", _DESCRIPTION, _PARAMETERS, run)
