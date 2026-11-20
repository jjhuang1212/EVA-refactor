"""终端渠道：读一行输入当作用户消息，打印工具调用过程和回复。

只认 contracts 里的 Reply 和 common 里的工具，不 import 核心的其他层。Ctrl+C 在回复期间是取消（turn 在步骤边界收尾），
在等输入时是退出。
"""
import threading
from pathlib import Path
from typing import Callable, List, Tuple

from eva.common.choices import numbered
from eva.contracts.channel import Reply
from eva.contracts.errors import EvaError

EXIT_WORDS = ("/exit", "/quit")
UPLOAD_COMMAND = "/upload "
_PREVIEW = 200


def render_process(events: List[dict]) -> List[str]:
    """把本 turn 的工具调用和结果排成行。回复正文由调用方单独打印。"""
    names, lines = {}, []
    for e in events:
        p = e["payload"]
        if e["event_type"] == "tool_call":
            names[p["call_id"]] = p["name"]
            lines.append(f"  → {p['name']} {p['arguments']}")
        elif e["event_type"] == "tool_result":
            content = str(p.get("content", ""))
            shown = content if len(content) <= _PREVIEW else content[:_PREVIEW] + "…"
            lines.append(f"  {'✗' if p.get('is_error') else '←'} {names.get(p['call_id']) or p.get('name') or p['call_id']}: {shown}")
    return lines


def render_error(e: EvaError) -> str:
    return f"[{e.code.value} {e.code.name}] {e.message}（{'可重试' if e.retryable else '不可重试'}）{e.details or ''}"


def _submit_cancellable(submit: Callable, text: str) -> Tuple[object, threading.Event]:
    """在工作线程里跑 submit，主线程等待。等待期间的 Ctrl+C 设置取消事件，继续等 turn 收尾。"""
    cancel, done, box = threading.Event(), threading.Event(), {}

    def work():
        try:
            box["value"] = submit(text, cancel)
        except BaseException as e:  # noqa: BLE001 线程边界：原样带回主线程再抛
            box["error"] = e
        finally:
            done.set()

    threading.Thread(target=work, name="console-turn", daemon=True).start()
    while not done.is_set():
        try:
            done.wait(0.2)
        except KeyboardInterrupt:
            cancel.set()
    if "error" in box:
        raise box["error"]
    return box["value"], cancel


def _upload(upload: Callable[[str, bytes], dict], arg: str, out: Callable) -> None:
    path = Path(arg.strip().strip('"'))
    try:
        data = path.read_bytes()
    except OSError as e:
        out(f"读不了文件 {path}：{e}")
        return
    try:
        info = upload(path.name, data)
    except EvaError as e:
        out(render_error(e))
        return
    out(f"已上传 {info['filename']}（{info['bytes']} 字节，file_id={info['file_id']}）")


def run(submit: Callable[[str, threading.Event], Reply], upload: Callable[[str, bytes], dict],
        input_fn: Callable = input, out: Callable = print) -> int:
    out("EVA console。输入问题回车发送，/upload 路径 上传文件，/exit 退出；回复期间 Ctrl+C 取消。")
    while True:
        try:
            text = input_fn("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if not text:
            continue
        if text in EXIT_WORDS:
            return 0
        if text.startswith(UPLOAD_COMMAND):
            _upload(upload, text[len(UPLOAD_COMMAND):], out)
            continue
        try:
            reply, cancel = _submit_cancellable(submit, text)
        except EvaError as e:
            out(render_error(e))
            continue
        for line in render_process(reply.events):
            out(line)
        shown = numbered(reply.text, reply.options) if reply.options else reply.text
        out(("? " if reply.reason == "waiting_user" else "") + (shown or "(无文字回复)"))
        out(f"[{reply.reason} · {reply.steps} 步 · ¥{reply.cost_cny}{' · 已取消' if cancel.is_set() else ''}]")
