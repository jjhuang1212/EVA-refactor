"""Redis 里的渠道状态：事件队列和消息队列（Stream 加消费组）、sync_msg 游标、下发窗口与配额。

客户端只在存储层。键都在 {prefix}:wecom: 之下，prefix 来自配置，新旧系统共用一个 Redis 时靠它隔开。
Redis 连不上或超时抛 E9001（可重试），其他 Redis 错误抛 E9002。
下发窗口和配额的数值由渠道传入，这里只负责原子地核对和计数。
"""
import os
from typing import Dict, List, Optional, Tuple

import redis
from dotenv import load_dotenv

from eva.contracts.errors import Code, EvaError

_GROUP = "workers"


def _translate(e: Exception) -> EvaError:
    if isinstance(e, (redis.ConnectionError, redis.TimeoutError)):
        return EvaError(Code.STORAGE_UNAVAILABLE, "Redis 连不上或超时", error=str(e))
    return EvaError(Code.STORAGE_ERROR, "Redis 错误", error_type=type(e).__name__, error=str(e))


def connect_from_env() -> "redis.Redis":
    """EVA_REDIS_HOST / PORT / DB 必填，EVA_REDIS_PASSWORD 可空。开发和测试先连本机，不复用旧系统的 REDIS_HOST。"""
    load_dotenv()
    missing = [n for n in ("EVA_REDIS_HOST", "EVA_REDIS_PORT", "EVA_REDIS_DB") if not os.environ.get(n)]
    if missing:
        raise EvaError(Code.STORAGE_ERROR, "缺少 Redis 环境变量", missing=missing)
    try:
        port, db = int(os.environ["EVA_REDIS_PORT"]), int(os.environ["EVA_REDIS_DB"])
    except ValueError as e:
        raise EvaError(Code.STORAGE_ERROR, "EVA_REDIS_PORT / EVA_REDIS_DB 必须是整数") from e
    return redis.Redis(host=os.environ["EVA_REDIS_HOST"], port=port, db=db,
                       password=os.environ.get("EVA_REDIS_PASSWORD") or None,
                       decode_responses=True, socket_timeout=10, socket_connect_timeout=5)


class RedisState:
    def __init__(self, client: "redis.Redis", prefix: str):
        if not prefix:
            raise EvaError(Code.CONFIG_INVALID, "Redis 键前缀不能为空")
        self._r, self._base = client, f"{prefix}:wecom"
        self._stream, self._messages, self._cursors = f"{self._base}:events", f"{self._base}:messages", f"{self._base}:cursor"
        for stream in (self._stream, self._messages):
            try:
                self._r.xgroup_create(stream, _GROUP, id="0", mkstream=True)
            except redis.ResponseError as e:
                if "BUSYGROUP" not in str(e):  # 组已存在是正常情况
                    raise _translate(e) from e
            except redis.RedisError as e:
                raise _translate(e) from e

    # ---- 事件队列：载的是"某客服号有新消息，凭这个 token 去拉" ----

    def push(self, token: str, open_kfid: str) -> None:
        try:
            self._r.xadd(self._stream, {"kf_token": token, "open_kfid": open_kfid})
        except redis.RedisError as e:
            raise _translate(e) from e

    def reserve(self, consumer: str, count: int, block_ms: int, claim_idle_ms: int) -> List[Tuple[str, Dict[str, str]]]:
        return self._reserve(self._stream, consumer, count, block_ms, claim_idle_ms)

    def ack(self, entry_id: str) -> None:
        try:
            self._r.xack(self._stream, _GROUP, entry_id)
        except redis.RedisError as e:
            raise _translate(e) from e

    # ---- 消息队列：载的是拉下来的一条条客户消息，按租户分派给处理线程 ----

    def enqueue(self, fields: Dict[str, str]) -> None:
        try:
            self._r.xadd(self._messages, fields)
        except redis.RedisError as e:
            raise _translate(e) from e

    def reserve_messages(self, consumer: str, count: int, block_ms: int, claim_idle_ms: int) -> List[Tuple[str, Dict[str, str]]]:
        return self._reserve(self._messages, consumer, count, block_ms, claim_idle_ms)

    def ack_message(self, entry_id: str) -> None:
        """确认并删除：消息只在处理完之前有用，留着只会让队列无限增长。"""
        try:
            pipe = self._r.pipeline()
            pipe.xack(self._messages, _GROUP, entry_id)
            pipe.xdel(self._messages, entry_id)
            pipe.execute()
        except redis.RedisError as e:
            raise _translate(e) from e

    def _reserve(self, stream: str, consumer: str, count: int, block_ms: int, claim_idle_ms: int):
        """先认领闲置太久还没确认的（上个消费者可能已崩溃），没有再读新的。"""
        try:
            claimed = self._r.xautoclaim(stream, _GROUP, consumer, claim_idle_ms, start_id="0-0", count=count)[1]
            if claimed:
                return [(i, f) for i, f in claimed if f is not None]
            got = self._r.xreadgroup(_GROUP, consumer, {stream: ">"}, count=count, block=block_ms)
        except redis.RedisError as e:
            raise _translate(e) from e
        return [(i, f) for _, entries in got for i, f in entries]

    # ---- 游标：先处理完消息，再保存 ----

    def get_cursor(self, open_kfid: str) -> str:
        """没存过返回空串，含义是"这个客服号从头拉"，不是故障。"""
        try:
            return self._r.hget(self._cursors, open_kfid) or ""
        except redis.RedisError as e:
            raise _translate(e) from e

    def save_cursor(self, open_kfid: str, cursor: str) -> None:
        try:
            self._r.hset(self._cursors, open_kfid, cursor)
        except redis.RedisError as e:
            raise _translate(e) from e

    def cursor_kfids(self) -> List[str]:
        """存过游标的客服号，对账只补拉它们。从没收到过消息的号没有游标，也就没有可补的。"""
        try:
            return sorted(self._r.hkeys(self._cursors))
        except redis.RedisError as e:
            raise _translate(e) from e

    # ---- 下发窗口与配额：按 (客服号, 用户) ----

    def _delivery_key(self, open_kfid: str, user: str) -> str:
        return f"{self._base}:delivery:{open_kfid}:{user}"

    def _held_key(self, open_kfid: str, user: str) -> str:
        return f"{self._base}:held:{open_kfid}:{user}"

    def note_inbound(self, open_kfid: str, user: str, send_time: int, window_seconds: float) -> None:
        """用户发来一条消息：窗口从这条的发送时间起算，已下发条数清零。只接受比已记录更新的，重投的旧消息不会重置配额。"""
        key = self._delivery_key(open_kfid, user)

        def txn(pipe):
            last = pipe.hget(key, "last_user_at")
            if last is not None and int(last) >= send_time:
                return
            pipe.multi()
            pipe.hset(key, mapping={"last_user_at": send_time, "sent": 0})
            pipe.expire(key, int(window_seconds))

        try:
            self._r.transaction(txn, key)
        except redis.RedisError as e:
            raise _translate(e) from e

    def delivery_left(self, open_kfid: str, user: str, now: float, window_seconds: float, quota: int) -> Optional[int]:
        """窗口内还能下发几条；窗口已过或从没收到过用户消息返回 None。"""
        try:
            state = self._r.hgetall(self._delivery_key(open_kfid, user))
        except redis.RedisError as e:
            raise _translate(e) from e
        if not state or now - int(state["last_user_at"]) >= window_seconds:
            return None
        return max(quota - int(state["sent"]), 0)

    def hold(self, open_kfid: str, user: str, item: str, window_seconds: float) -> None:
        """配额用完时放不下的回复，等用户再发消息后补发。和窗口同寿命：窗口过了用户也没法再收到。"""
        key = self._held_key(open_kfid, user)
        try:
            pipe = self._r.pipeline()
            pipe.rpush(key, item)
            pipe.expire(key, int(window_seconds))
            pipe.execute()
        except redis.RedisError as e:
            raise _translate(e) from e

    def take_held(self, open_kfid: str, user: str) -> List[str]:
        """取走并清空暂存的回复，按暂存先后。"""
        key = self._held_key(open_kfid, user)
        try:
            pipe = self._r.pipeline()
            pipe.lrange(key, 0, -1)
            pipe.delete(key)
            return pipe.execute()[0]
        except redis.RedisError as e:
            raise _translate(e) from e

    def reserve_delivery(self, open_kfid: str, user: str, n: int, now: float, window_seconds: float,
                         quota: int) -> Optional[str]:
        """要下发 n 条。窗口内且配额够就计数并返回 None；否则不计数，返回 "window" 或 "quota"。"""
        key = self._delivery_key(open_kfid, user)
        verdict: List[Optional[str]] = []

        def txn(pipe):
            verdict.clear()
            state = pipe.hgetall(key)
            if not state or now - int(state["last_user_at"]) >= window_seconds:
                verdict.append("window")
                return
            if int(state["sent"]) + n > quota:
                verdict.append("quota")
                return
            pipe.multi()
            pipe.hincrby(key, "sent", n)
            verdict.append(None)

        try:
            self._r.transaction(txn, key)
        except redis.RedisError as e:
            raise _translate(e) from e
        return verdict[0]
