"""运行日志：给开发和排障看进程里发生了什么，和 transcript（审计、计费的事实来源）是两回事。

终端输出中文单行；文件输出 JSON Lines，按天切分。时间一律经 clock，东八区。
INFO 以上不带正文：用户消息、模型回复、文件内容是租户私有数据，只记长度和 ID；
preview_chars 大于 0 时才附截断的预览，仅供本机调试。
"""
import contextvars
import json
import logging
import logging.handlers
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from eva.common import clock

# 只给 eva 和 tools 开到配置的级别；第三方库保持 WARNING，免得 HTTP 客户端的细节刷屏
_OWN_NAMESPACES = ("eva", "tools")
_QUIET = {"werkzeug": logging.WARNING}
_COLORS = {logging.DEBUG: "\033[90m", logging.INFO: "\033[0m", logging.WARNING: "\033[33m",
           logging.ERROR: "\033[31m", logging.CRITICAL: "\033[1;31m"}
_RESET = "\033[0m"

_ctx: contextvars.ContextVar = contextvars.ContextVar("eva_log_ctx", default={})
_preview_chars = 0


@dataclass(frozen=True)
class LogConfig:
    console_level: int
    file_level: int
    directory: Path
    keep_days: int
    color: bool
    preview_chars: int


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """这段代码里打出的每条日志都带上这些字段（租户、会话、消息）。线程池里要用 copy_context 才能带过去。"""
    token = _ctx.set({**_ctx.get(), **fields})
    try:
        yield
    finally:
        _ctx.reset(token)


def preview(text: Any) -> str:
    """正文预览：折成一行并截断。preview_chars 为 0 时返回空串，日志里就不出现这个字段。"""
    if _preview_chars <= 0 or text is None:
        return ""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= _preview_chars else flat[:_preview_chars] + "…"


def emit(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": fields})


def report_error(logger: logging.Logger, error: Any, **fields: Any) -> None:
    """记一条带错误码的 ERROR。error 是 EvaError（只用它的 to_dict），common 层不 import contracts。"""
    d = error.to_dict()
    emit(logger, logging.ERROR, d["message"] or d["name"], code=d["code"], name=d["name"],
         retryable=d["retryable"], details=d["details"], **fields)


def _fields(record: logging.LogRecord) -> dict:
    return {k: v for k, v in getattr(record, "fields", {}).items() if v not in ("", None)}


def _show(v: Any) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    s = str(v)
    return f"“{s}”" if any(c.isspace() for c in s) else s


class ConsoleFormatter(logging.Formatter):
    def __init__(self, color: bool):
        super().__init__()
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        ctx = _ctx.get()
        who = f" {str(ctx['tenant'])[:8]}" if ctx.get("tenant") else ""
        extra = "".join(f"  {k}={_show(v)}" for k, v in _fields(record).items())
        line = f"{clock.from_unix(record.created):%H:%M:%S} {record.levelname[:4]}{who} {record.getMessage()}{extra}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return f"{_COLORS.get(record.levelno, '')}{line}{_RESET}" if self._color else line


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        row = {"ts": clock.from_unix(record.created).isoformat(sep=" ", timespec="milliseconds"),
               "level": record.levelname, "logger": record.name, "msg": record.getMessage(),
               **_ctx.get(), **_fields(record)}
        if record.exc_info:
            row["exc"] = self.formatException(record.exc_info)
        return json.dumps(row, ensure_ascii=False, default=str)


def setup(cfg: LogConfig, name: str, terminal: bool = True) -> None:
    """装好终端和文件两个 handler，可重复调用。日志目录建不出来或文件打不开时抛 OSError，由调用方转成错误码。
    name 决定文件名（logs/<name>.log），同时跑的几个进程各写各的，不抢同一个文件。"""
    global _preview_chars
    _preview_chars = cfg.preview_chars
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_eva", False)]:
        root.removeHandler(h)
        h.close()
    handlers = []
    cfg.directory.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        cfg.directory / f"{name}.log", when="midnight", backupCount=cfg.keep_days, encoding="utf-8")
    file_handler.setLevel(cfg.file_level)
    file_handler.setFormatter(JsonFormatter())
    handlers.append(file_handler)
    if terminal:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(cfg.console_level)
        console.setFormatter(ConsoleFormatter(cfg.color))
        handlers.append(console)
    for h in handlers:
        h._eva = True
        root.addHandler(h)
    root.setLevel(logging.WARNING)
    for ns in _OWN_NAMESPACES:
        logging.getLogger(ns).setLevel(min(cfg.console_level if terminal else cfg.file_level, cfg.file_level))
    for ns, level in _QUIET.items():
        logging.getLogger(ns).setLevel(level)
