"""子进程的受限运行：超时、输出字节上限、取消，三者都会杀掉整棵进程树。两个后端共用。"""
import os
import signal
import subprocess
import threading
import time
from typing import Callable, Mapping, Optional, Sequence, Tuple

from eva.contracts.errors import Code, EvaError

_CHUNK = 8192
_POLL_SECONDS = 0.05


def tail(data: bytes, n: int = 500) -> str:
    return data[-n:].decode("utf-8", errors="replace")


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)  # 进程已退出时返回非 0，无需处理
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # start_new_session 让它自成一组
        except ProcessLookupError:
            pass  # 整组已经退出
    proc.kill()


class _Capture(threading.Thread):
    """读一路管道，超过上限就停读并置位 over。"""

    def __init__(self, pipe, limit: int, over: threading.Event):
        super().__init__(daemon=True)
        self._pipe, self._limit, self._over = pipe, limit, over
        self.data = bytearray()

    def run(self) -> None:
        while True:
            chunk = self._pipe.read1(_CHUNK)  # read 会等凑满 _CHUNK 才返回，超限和超时时的输出就读不到
            if not chunk:
                return
            self.data += chunk
            if len(self.data) > self._limit:
                self._over.set()
                return


def run_capped(argv: Sequence[str], cwd, env: Mapping[str, str], timeout: float, max_bytes: int,
               cancel: threading.Event, on_kill: Optional[Callable[[], None]] = None) -> Tuple[int, bytes, bytes]:
    """返回 (退出码, stdout, stderr)。启动失败 E8001；超时 E8002；任一路输出超限 E8003；取消 E6006。
    on_kill 在杀进程树之后调用，容器后端借它按名字删容器。"""
    extra = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    try:
        proc = subprocess.Popen(list(argv), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **extra)
    except OSError as e:
        raise EvaError(Code.SANDBOX_UNAVAILABLE, "沙箱进程启动失败", argv0=str(argv[0]), error=str(e)) from e
    over = threading.Event()
    out, err = _Capture(proc.stdout, max_bytes, over), _Capture(proc.stderr, max_bytes, over)
    out.start()
    err.start()
    deadline = time.monotonic() + timeout
    problem = None
    while proc.poll() is None and problem is None:
        if over.is_set():
            problem = "output"
        elif cancel.is_set():
            problem = "cancel"
        elif time.monotonic() >= deadline:
            problem = "timeout"
        else:
            time.sleep(_POLL_SECONDS)
    if problem is not None or out.is_alive() or err.is_alive():
        out.join(1)
        err.join(1)
    if problem is not None or out.is_alive() or err.is_alive():  # 超限、取消、超时，或孙进程还攥着管道
        _kill_tree(proc)
        if on_kill is not None:
            on_kill()
        out.join(2)
        err.join(2)
    proc.wait()
    for reader, pipe in ((out, proc.stdout), (err, proc.stderr)):
        if not reader.is_alive():  # 孙进程还攥着管道时，读线程在读，关管道会和它互相等待
            pipe.close()
    if problem == "timeout":
        raise EvaError(Code.SANDBOX_TIMEOUT, "代码运行超时，已终止", timeout_seconds=timeout,
                       stdout_tail=tail(bytes(out.data)), stderr_tail=tail(bytes(err.data)))
    if problem == "cancel":
        raise EvaError(Code.TOOL_ABORTED, "取消，沙箱运行已终止")
    if problem == "output" or len(out.data) > max_bytes or len(err.data) > max_bytes:
        raise EvaError(Code.SANDBOX_OUTPUT_LIMIT, "stdout 或 stderr 超过字节上限，已终止", reason="output",
                       max_bytes=max_bytes, stdout_bytes=len(out.data), stderr_bytes=len(err.data),
                       stdout_tail=tail(bytes(out.data)), stderr_tail=tail(bytes(err.data)))
    return proc.returncode, bytes(out.data), bytes(err.data)
