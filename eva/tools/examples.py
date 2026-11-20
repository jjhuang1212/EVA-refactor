"""最小示例工具，用来验证"调用、结果、继续回答"这条链路。"""
from eva.common import clock
from eva.contracts.vocab import Concurrency
from eva.tools.base import Tool

get_time = Tool(
    name="get_time",
    description="获取当前的日期和时间（东八区）。",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    run=lambda args, ctx: clock.now().strftime("%Y-%m-%d %H:%M:%S"),
    concurrency=Concurrency.SHARED,
)
