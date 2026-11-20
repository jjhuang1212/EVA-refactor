"""sessions 表。"""
from typing import List, Optional, Tuple

from eva.common import clock, ids
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import Channel, check
from eva.storage.repo import FK_VIOLATION, TenantRepo

_NAMES = ("session_id", "tenant_id", "channel", "parent_session_id", "created_at")
_COLS = ", ".join(_NAMES)


class Sessions(TenantRepo):
    def get(self, session_id: str) -> Optional[dict]:
        r = self._execute(
            f"SELECT {_COLS} FROM sessions WHERE tenant_id = %s AND session_id = %s", (self.tenant_id, session_id)
        ).fetchone()
        return dict(zip(_NAMES, r)) if r else None

    def require(self, session_id: str) -> dict:
        """别的租户的会话和不存在的会话都报 E3002，不区分。"""
        found = self.get(session_id)
        if found is None:
            raise EvaError(Code.SESSION_NOT_FOUND, "会话不存在", session_id=session_id)
        return found

    def create(self, channel: str) -> str:
        return self._insert(check(Channel, channel).value, None)

    def _insert(self, channel: str, parent_session_id: Optional[str]) -> str:
        session_id = ids.new_id()
        self._execute(
            "INSERT INTO sessions (session_id, tenant_id, channel, parent_session_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (session_id, self.tenant_id, channel, parent_session_id, clock.now()),
            on_integrity={FK_VIOLATION: EvaError(Code.TENANT_NOT_FOUND, "租户不存在", tenant_id=self.tenant_id)},
        )
        return session_id

    def create_child(self, parent_session_id: str) -> str:
        """子会话复制父的渠道；父会话必须是本租户的。"""
        parent = self.require(parent_session_id)
        return self._insert(parent["channel"], parent_session_id)

    def latest(self, channel: str) -> Optional[dict]:
        """某渠道下本租户最新建的会话，不含长任务的子会话。没有则返回 None。"""
        r = self._execute(
            f"SELECT TOP 1 {_COLS} FROM sessions WHERE tenant_id = %s AND channel = %s AND parent_session_id IS NULL "
            "ORDER BY created_at DESC, session_id DESC",
            (self.tenant_id, check(Channel, channel).value),
        ).fetchone()
        return dict(zip(_NAMES, r)) if r else None

    def list_recent(self, limit: int, offset: int = 0) -> Tuple[List[dict], bool]:
        """本租户最近有动静的会话，新的在前，附 last_active_at 和已结算费用 cost_cny。
        返回 (本页行, 是否还有下一页)。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise EvaError(Code.INVALID_VALUE, "limit 必须是正整数", limit=limit)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise EvaError(Code.INVALID_VALUE, "offset 必须是非负整数", offset=offset)
        cur = self._execute(
            f"SELECT {', '.join('s.' + n for n in _NAMES)}, "
            "ISNULL((SELECT MAX(t.created_at) FROM transcript_events t "
            "WHERE t.session_id = s.session_id AND t.tenant_id = s.tenant_id), s.created_at) AS last_active_at, "
            "(SELECT ISNULL(SUM(l.amount_cny), 0) FROM ledger_events l WHERE l.tenant_id = s.tenant_id "
            "AND l.session_id = s.session_id AND l.kind = 'settle') AS cost_cny "
            "FROM sessions s WHERE s.tenant_id = %s ORDER BY last_active_at DESC, s.session_id DESC "
            "OFFSET %s ROWS FETCH NEXT %s ROWS ONLY",
            (self.tenant_id, offset, limit + 1))
        rows = [dict(zip(_NAMES + ("last_active_at", "cost_cny"), r)) for r in cur.fetchall()]
        return rows[:limit], len(rows) > limit

    def find(self, channel: str) -> List[dict]:
        """某渠道下本租户的全部会话，按创建时间升序。没有则返回空列表。"""
        cur = self._execute(
            f"SELECT {_COLS} FROM sessions WHERE tenant_id = %s AND channel = %s ORDER BY created_at, session_id",
            (self.tenant_id, check(Channel, channel).value),
        )
        return [dict(zip(_NAMES, r)) for r in cur.fetchall()]
