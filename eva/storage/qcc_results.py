"""qcc_results 表：query_company 给模型看过的企业数据记录，编号在会话内唯一，只追加。"""
from datetime import datetime
from typing import List, Optional

from eva.storage.repo import TenantRepo
from eva.storage.sessions import Sessions

_NAMES = ("ref_no", "call_id", "uscc", "dimension", "person", "record_no", "content", "fetched_at")
_COLS = ", ".join(_NAMES)


class QccResults(TenantRepo):
    def add(self, session_id: str, call_id: str, uscc: str, dimension: str, person: Optional[str], contents: List[str],
            fetched_at: datetime) -> List[int]:
        """存一批记录，fetched_at 是向 QCC 取数的时间（缓存命中时是当初取数的时间）。返回分配的编号（接在会话已有的最大编号后面）。会话不属于本租户抛 E3002。
        对该会话的编号范围加锁后再取最大值，同一会话并行的查询不会拿到相同的编号。"""
        Sessions(self.conn, self.tenant_id).require(session_id)
        top = self._execute(
            "SELECT ISNULL(MAX(ref_no), 0) FROM qcc_results WITH (UPDLOCK, HOLDLOCK) WHERE tenant_id = %s AND session_id = %s",
            (self.tenant_id, session_id)).fetchone()[0]
        numbers = []
        for offset, content in enumerate(contents, start=1):
            numbers.append(top + offset)
            self._execute(
                f"INSERT INTO qcc_results (tenant_id, session_id, {_COLS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (self.tenant_id, session_id, top + offset, call_id, uscc, dimension, person, offset, content, fetched_at))
        return numbers

    def list(self, session_id: str) -> List[dict]:
        rows = self._execute(f"SELECT {_COLS} FROM qcc_results WHERE tenant_id = %s AND session_id = %s ORDER BY ref_no",
                             (self.tenant_id, session_id)).fetchall()
        return [dict(zip(_NAMES, r)) for r in rows]
