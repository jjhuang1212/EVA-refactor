"""transcript_events 表：只追加，没有 UPDATE 和 DELETE。"""
import json
from typing import List, Optional

from eva.common import clock
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import EventType, check
from eva.storage.repo import UNIQUE_VIOLATION, TenantRepo

# 条件插入：会话属于本租户，且期望序号恰好等于下一个序号。两个 worker 抢同一个序号时由主键兜底
_APPEND = (
    "INSERT INTO transcript_events (session_id, seq, tenant_id, event_type, payload, created_at) "
    "SELECT %s, %s, %s, %s, %s, %s "
    "WHERE EXISTS (SELECT 1 FROM sessions WHERE session_id = %s AND tenant_id = %s) "
    "AND %s = (SELECT ISNULL(MAX(seq) + 1, 0) FROM transcript_events WHERE session_id = %s AND tenant_id = %s)"
)

# 起点 = max(最近一次重置的下一个序号, 重置之后最近一次成功压缩的 cut_seq)。r 和 c 各是一个只有一行的子查询
_LIVE = (
    "WITH r AS (SELECT ISNULL(MAX(seq) + 1, 0) AS s FROM transcript_events "
    "WHERE session_id = %s AND tenant_id = %s AND event_type = 'context_reset'), "
    "c AS (SELECT ISNULL((SELECT TOP 1 CAST(JSON_VALUE(payload, '$.cut_seq') AS INT) FROM transcript_events "
    "WHERE session_id = %s AND tenant_id = %s AND event_type = 'compaction_end' "
    "AND JSON_VALUE(payload, '$.status') = 'ok' AND seq >= (SELECT s FROM r) ORDER BY seq DESC), 0) AS cut) "
    "SELECT e.seq, e.event_type, e.payload, e.created_at FROM transcript_events e, r, c "
    "WHERE e.session_id = %s AND e.tenant_id = %s AND e.seq >= CASE WHEN r.s > c.cut THEN r.s ELSE c.cut END ORDER BY e.seq"
)


class Transcript(TenantRepo):
    def next_seq(self, session_id: str) -> int:
        return self._execute(
            "SELECT ISNULL(MAX(seq) + 1, 0) FROM transcript_events WHERE session_id = %s AND tenant_id = %s",
            (session_id, self.tenant_id),
        ).fetchone()[0]

    def append(self, session_id: str, expected_seq: int, event_type: str, payload: dict) -> int:
        """期望序号对不上抛 E3001（重读后可重试），会话不属于本租户抛 E3002。返回写入的序号。"""
        event_type = check(EventType, event_type).value
        if not isinstance(expected_seq, int) or isinstance(expected_seq, bool) or expected_seq < 0:
            raise EvaError(Code.INVALID_VALUE, "expected_seq 必须是非负整数", value=expected_seq)
        try:
            body = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            raise EvaError(Code.INVALID_VALUE, "payload 不能序列化为 JSON", reason=str(e)) from e

        conflict = EvaError(Code.SEQ_CONFLICT, "序号已被占用", session_id=session_id, expected_seq=expected_seq)
        cur = self._execute(
            _APPEND,
            (session_id, expected_seq, self.tenant_id, event_type, body, clock.now(),
             session_id, self.tenant_id, expected_seq, session_id, self.tenant_id),
            on_integrity={n: conflict for n in UNIQUE_VIOLATION},
        )
        if cur.rowcount == 1:
            return expected_seq
        owned = self._execute(
            "SELECT 1 FROM sessions WHERE session_id = %s AND tenant_id = %s", (session_id, self.tenant_id)
        ).fetchone()
        if owned is None:
            raise EvaError(Code.SESSION_NOT_FOUND, "会话不存在", session_id=session_id)
        raise EvaError(
            Code.SEQ_CONFLICT, "期望序号不等于下一个序号",
            session_id=session_id, expected_seq=expected_seq, next_seq=self.next_seq(session_id),
        )

    def read(self, session_id: str, from_seq: int = 0) -> List[dict]:
        """序号 >= from_seq 的事件，按序号升序。会话不存在或不是本租户的返回空列表。"""
        cur = self._execute(
            "SELECT seq, event_type, payload, created_at FROM transcript_events "
            "WHERE session_id = %s AND tenant_id = %s AND seq >= %s ORDER BY seq",
            (session_id, self.tenant_id, from_seq),
        )
        return [
            {"seq": seq, "event_type": et, "payload": json.loads(p), "created_at": ca}
            for seq, et, p, ca in cur.fetchall()
        ]

    def read_live(self, session_id: str) -> List[dict]:
        """模型视图用得到的事件：最近一次 context_reset 之后，且从最近一次成功压缩的切点起。更早的事件被摘要代替，
        永续会话里读它们只会越来越慢。一次查询取回，不另外先查起点。"""
        cur = self._execute(_LIVE, (session_id, self.tenant_id) * 3)
        return [{"seq": seq, "event_type": et, "payload": json.loads(p), "created_at": ca} for seq, et, p, ca in cur.fetchall()]

    def read_since_reset(self, session_id: str) -> List[dict]:
        """最近一次 context_reset 之后的全部事件（不按压缩截断）；没有重置过就是整个会话。"""
        row = self._execute(
            "SELECT MAX(seq) FROM transcript_events WHERE session_id = %s AND tenant_id = %s AND event_type = 'context_reset'",
            (session_id, self.tenant_id)).fetchone()
        return self.read(session_id, 0 if row[0] is None else row[0] + 1)

    def current_turn(self, session_id: str) -> List[dict]:
        """最近一个 turn（从它的 turn_start 起）的全部事件；还没有 turn 返回整个会话。"""
        row = self._execute(
            "SELECT MAX(seq) FROM transcript_events WHERE session_id = %s AND tenant_id = %s AND event_type = 'turn_start'",
            (session_id, self.tenant_id)).fetchone()
        return self.read(session_id, row[0] or 0)

    def since_last_turn_end(self, session_id: str) -> List[dict]:
        """上一个 turn 结束之后的全部事件：这一轮的用户输入（在 turn_start 之前写入）和这一轮已有的事件。
        还没有结束过的 turn 返回整个会话。"""
        row = self._execute(
            "SELECT MAX(seq) FROM transcript_events WHERE session_id = %s AND tenant_id = %s AND event_type = 'turn_end'",
            (session_id, self.tenant_id)).fetchone()
        return self.read(session_id, 0 if row[0] is None else row[0] + 1)

    def find_user_message(self, session_id: str, msg_id: str) -> Optional[int]:
        """按渠道消息 ID 找已记过的用户输入（用户消息，或写成 ask_user 结果的回答），返回序号；没有返回 None。
        渠道可能重复投递，追加前用它去重。"""
        if not isinstance(msg_id, str) or not msg_id:
            raise EvaError(Code.INVALID_VALUE, "msg_id 不能为空", value=repr(msg_id))
        row = self._execute(
            "SELECT TOP 1 seq FROM transcript_events WHERE session_id = %s AND tenant_id = %s "
            "AND event_type IN ('user_message', 'tool_result') AND JSON_VALUE(payload, '$.msg_id') = %s ORDER BY seq",
            (session_id, self.tenant_id, msg_id),
        ).fetchone()
        return row[0] if row else None

