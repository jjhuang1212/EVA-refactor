"""进程级熔断器，按模型名一个。连续失败达到阈值就断开，到时间后放一个探针，探针成功才合上。

进程级的原因：一次失败的判断要对后续所有并发调用生效，否则每个并发槽各自撞一遍坏掉的主选。
"""
import threading
import time
from typing import Callable, Dict


class Breaker:
    def __init__(self, failures: int, probe_after_seconds: float, now: Callable[[], float] = time.monotonic):
        self._limit, self._probe_after, self._now = failures, probe_after_seconds, now
        self._lock = threading.Lock()
        self._fails = 0
        self._opened_at = None  # None 表示合上
        self._probing = False

    def allow(self) -> bool:
        """能不能发请求。断开期间只在到点后放行一个探针，其余一律拒绝。"""
        with self._lock:
            if self._opened_at is None:
                return True
            if not self._probing and self._now() - self._opened_at >= self._probe_after:
                self._probing = True
                return True
            return False

    def success(self) -> None:
        with self._lock:
            self._fails, self._opened_at, self._probing = 0, None, False

    def failure(self) -> None:
        with self._lock:
            self._fails += 1
            if self._probing or self._fails >= self._limit:
                self._opened_at, self._probing = self._now(), False


class Breakers:
    def __init__(self, failures: int, probe_after_seconds: float, now: Callable[[], float] = time.monotonic):
        self._args = (failures, probe_after_seconds, now)
        self._by_model: Dict[str, Breaker] = {}
        self._lock = threading.Lock()

    def get(self, model: str) -> Breaker:
        with self._lock:
            return self._by_model.setdefault(model, Breaker(*self._args))
