"""全代码库唯一取当前时间的地方。

数据库时间列是 datetime2，存东八区本地时间且不带偏移，所以这里返回 naive datetime。
其他地方不得直接调 datetime.now()。
"""
import time
from datetime import datetime, timedelta, timezone

_CST = timezone(timedelta(hours=8))


def now() -> datetime:
    return datetime.now(_CST).replace(tzinfo=None)


def day_start() -> datetime:
    """东八区今天 0 点，按天计的额度从这里起算。"""
    return now().replace(hour=0, minute=0, second=0, microsecond=0)


def unix() -> float:
    """当前 Unix 时间戳（秒），用于和渠道给的时间戳比较。"""
    return time.time()


def from_unix(ts: float) -> datetime:
    """企微回调里的 Unix 时间戳转东八区。"""
    return datetime.fromtimestamp(ts, _CST).replace(tzinfo=None)
