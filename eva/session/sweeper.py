"""预扣清扫：worker 崩溃会留下"有预扣无结算"，超过配置时长的由这里补一条 release，把额度还回去。

进程启动时先清扫一次（覆盖上次崩溃留下的），之后周期清扫（覆盖运行中释放失败的，例如存储短暂不可用）。
"""
import logging
import threading
from datetime import timedelta
from typing import Callable, Optional

from eva.common import clock, logs
from eva.contracts.errors import Code, EvaError
from eva.session.config import SessionConfig
from eva.storage import db
from eva.storage.ledger import Ledger, stale_holds

log = logging.getLogger(__name__)
_RACED = (Code.DUPLICATE_IDEM, Code.HOLD_CLOSED)  # 清扫的同时有人正常结算或释放了，不算故障


def sweep_once(cfg: SessionConfig, open_tx: Callable = db.transaction, now: Callable = clock.now) -> int:
    """释放所有过期预扣，返回释放了几条。每条在各自的租户下、各自的事务里释放。"""
    with open_tx() as conn:
        stale = stale_holds(conn, now() - timedelta(seconds=cfg.stale_hold_seconds), cfg.sweep_batch)
    released = 0
    for tenant_id, hold_id in stale:
        try:
            with open_tx() as conn:
                Ledger(conn, tenant_id).release(hold_id, f"sweep:{hold_id}")
        except EvaError as e:
            if e.code in _RACED:
                continue
            raise
        released += 1
    return released


class Sweeper:
    """后台周期清扫。last_error 是最近一轮的失败（成功后清空），入口进程可以据此告警。"""

    def __init__(self, cfg: SessionConfig, open_tx: Callable = db.transaction):
        self._cfg, self._open_tx = cfg, open_tx
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_error: Optional[EvaError] = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Sweeper 已启动")
        self._thread = threading.Thread(target=self._run, name="hold-sweeper", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                released = sweep_once(self._cfg, self._open_tx)
                self.last_error = None
                if released:
                    logs.emit(log, logging.INFO, "释放过期预扣", released=released)
            except EvaError as e:  # 存储故障下一轮再试；其他异常是 bug，让线程带着堆栈退出
                self.last_error = e
                logs.report_error(log, e, where="sweeper")
            self._stop.wait(self._cfg.sweep_interval_seconds)
