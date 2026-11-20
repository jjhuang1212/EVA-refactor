"""按 key 分道的执行器：同一 key 的任务一个接一个按加入顺序跑，不同 key 并行，同时在跑的 key 不超过 workers。

企微渠道用租户做 key：一个用户的消息必须按顺序处理（同一会话只有一个写入者），不同用户互不等待。
"""
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional


class Lanes:
    """run(key, item) 返回 True 继续本道的下一个，返回 False 放弃本道还没跑的任务（交给 drop）。
    run 抛出的任何异常都是 bug：记在 fatal 里，所有道随即放弃剩余任务，由调用方取走再抛。
    drop 在内部锁里调用，必须很快，且不能回调 Lanes。"""

    def __init__(self, workers: int, run: Callable[[str, object], bool], drop: Callable[[str, List[object]], None]):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lane")
        self._run, self._drop = run, drop
        self._lanes: Dict[str, deque] = {}  # 在表里的 key 就是已有线程在管的道
        self._count = 0  # 已加入、还没跑完也没被放弃的任务数
        self._cond = threading.Condition()
        self.fatal: Optional[BaseException] = None

    def add(self, key: str, item: object) -> None:
        with self._cond:
            self._count += 1
            lane = self._lanes.get(key)
            if lane is not None:
                lane.append(item)
                return
            self._lanes[key] = deque([item])
        self._pool.submit(self._drain, key)

    def pending(self) -> int:
        with self._cond:
            return self._count

    def wait_idle(self) -> None:
        with self._cond:
            self._cond.wait_for(lambda: not self._lanes)

    def close(self) -> None:
        self._pool.shutdown(wait=True)

    def _abandon(self, key: str, lane: deque) -> None:
        rest = list(lane)
        lane.clear()
        self._count -= len(rest)
        self._drop(key, rest)

    def _drain(self, key: str) -> None:
        while True:
            with self._cond:
                lane = self._lanes[key]
                if self.fatal is not None:
                    self._abandon(key, lane)
                if not lane:
                    del self._lanes[key]
                    self._cond.notify_all()
                    return
                item = lane.popleft()
            try:
                keep = self._run(key, item)
            except BaseException as e:  # noqa: BLE001 工具边界：留给调用方原样抛，不在线程里吞掉
                keep = False
                with self._cond:
                    self.fatal = self.fatal or e
            with self._cond:
                self._count -= 1
                if not keep:
                    self._abandon(key, lane)
