"""web_results 表：联网搜索给模型看过的结果，编号在会话内唯一，只追加。"""
from typing import List, Optional

from eva.common import clock
from eva.storage.repo import TenantRepo
from eva.storage.sessions import Sessions

_NAMES = ("ref_no", "call_id", "title", "media", "publish_date", "link", "content")
_COLS = ", ".join(_NAMES)


class WebResults(TenantRepo):
    def add(self, session_id: str, call_id: str, rows: List[dict]) -> List[int]:
        """存一批结果，返回分配的编号（接在会话已有的最大编号后面）。会话不属于本租户抛 E3002。
        对该会话的编号范围加锁后再取最大值，同一会话并行的搜索不会拿到相同的编号。"""
        Sessions(self.conn, self.tenant_id).require(session_id)
        top = self._execute(
            "SELECT ISNULL(MAX(ref_no), 0) FROM web_results WITH (UPDLOCK, HOLDLOCK) WHERE tenant_id = %s AND session_id = %s",
            (self.tenant_id, session_id)).fetchone()[0]
        now, numbers = clock.now(), []
        for offset, row in enumerate(rows, start=1):
            numbers.append(top + offset)
            self._execute(
                f"INSERT INTO web_results (tenant_id, session_id, {_COLS}, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (self.tenant_id, session_id, top + offset, call_id, row["title"], row.get("media"), row.get("publish_date"),
                 row["link"], row["content"], now))
        return numbers

    def get(self, session_id: str, ref_no: int) -> Optional[dict]:
        r = self._execute(f"SELECT {_COLS} FROM web_results WHERE tenant_id = %s AND session_id = %s AND ref_no = %s",
                          (self.tenant_id, session_id, ref_no)).fetchone()
        return dict(zip(_NAMES, r)) if r else None

    def list(self, session_id: str) -> List[dict]:
        rows = self._execute(f"SELECT {_COLS} FROM web_results WHERE tenant_id = %s AND session_id = %s ORDER BY ref_no",
                             (self.tenant_id, session_id)).fetchall()
        return [dict(zip(_NAMES, r)) for r in rows]
