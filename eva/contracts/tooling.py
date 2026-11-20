"""工具、hook、循环三方共用的数据形状。只依赖标准库。"""
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ToolContext:
    """一次工具调用的运行上下文。cancel 被置位后，会等待的工具应尽快返回。"""
    tenant_id: str
    session_id: str
    call_id: str
    cancel: threading.Event
