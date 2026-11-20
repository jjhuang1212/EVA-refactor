"""工具调度：连续的 shared 工具成一批并行，其余各自独占。结果按模型给出的顺序返回。

线程只用在这里，循环主体和工具接口都不碰线程；以后换成异步只改这一个文件。
"""
import contextvars
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional

from eva.contracts.errors import Code, EvaError
from eva.contracts.tooling import ToolCall, ToolContext
from eva.contracts.vocab import Concurrency
from eva.tools.base import Tool


@dataclass
class Item:
    call: ToolCall
    ctx: ToolContext
    tool: Optional[Tool] = None
    result: Optional[dict] = None  # 已有结果（参数不合法、被拒绝等）就不再执行


def error_payload(call_id: str, e: EvaError) -> dict:
    d = e.to_dict()
    return {"call_id": call_id, "content": json.dumps(d, ensure_ascii=False), "is_error": True, "error": d}


def partition(items: List[Item]) -> List[List[Item]]:
    batches: List[List[Item]] = []
    for it in items:
        shared = it.result is None and it.tool.concurrency is Concurrency.SHARED
        if shared and batches and batches[-1][0].result is None \
                and batches[-1][0].tool.concurrency is Concurrency.SHARED:
            batches[-1].append(it)
        else:
            batches.append([it])
    return batches


def _run_one(it: Item) -> dict:
    """工具抛 EvaError 原样结构化；抛其他异常说明工具有 bug，包成 E6001 交给模型，类型和消息都留在 details。"""
    try:
        out = it.tool.run(it.call.arguments, it.ctx)
        content = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    except EvaError as e:
        return error_payload(it.call.id, e)
    except Exception as e:  # noqa: BLE001 工具边界：不能让一个工具的 bug 带走整个 turn
        return error_payload(it.call.id, EvaError(Code.TOOL_FAILED, str(e), tool=it.call.name,
                                                  error_type=type(e).__name__))
    return {"call_id": it.call.id, "content": content, "is_error": False}


class Scheduler:
    def __init__(self, max_parallel: int):
        self._pool = ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="tool")

    def execute(self, batch: List[Item]) -> List[dict]:
        """批内要执行的项（result 为空的）并行跑完，返回与 batch 等长、同序的结果。"""
        todo = [it for it in batch if it.result is None]
        # 日志上下文（租户、会话）靠 contextvars，线程池里要逐个带过去
        done = iter(self._pool.map(lambda ctx, it: ctx.run(_run_one, it),
                                   [contextvars.copy_context() for _ in todo], todo))
        return [it.result if it.result is not None else next(done) for it in batch]

    def close(self) -> None:
        self._pool.shutdown(wait=True)


def aborted_payload(call_id: str) -> dict:
    return error_payload(call_id, EvaError(Code.TOOL_ABORTED, "已取消，该调用未执行"))


