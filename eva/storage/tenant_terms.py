"""tenant_terms 表：租户私有的已确认写法。只属于这个租户，不进公共热词表，也不影响别的租户。"""
from typing import List, Optional

from eva.common import clock
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import TermKind, check
from eva.storage.repo import TenantRepo
from eva.storage.sessions import Sessions


class TenantTerms(TenantRepo):
    def confirm(self, term: str, kind: str, heard_as: Optional[str], session_id: Optional[str], max_terms: int,
                max_chars: int) -> bool:
        """记下一条确认过的写法，同一个写法再次确认覆盖原行。返回是否新增。已满 max_terms 条的新写法拒绝（E0001）。"""
        check(TermKind, kind)
        for label, value in (("term", term), ("heard_as", heard_as)):
            if value is None and label == "heard_as":
                continue
            if not isinstance(value, str) or not value.strip() or len(value) > max_chars or "\n" in value or "\r" in value:
                raise EvaError(Code.INVALID_VALUE, f"{label} 必须是不超过 {max_chars} 字的单行非空文字", field=label)
        if session_id is not None:
            Sessions(self.conn, self.tenant_id).require(session_id)
        # 对本租户的行加锁后再数，并发确认不会一起越过上限
        count = self._execute("SELECT COUNT(*) FROM tenant_terms WITH (UPDLOCK, HOLDLOCK) WHERE tenant_id = %s",
                              (self.tenant_id,)).fetchone()[0]
        updated = self._execute("UPDATE tenant_terms SET heard_as = %s, kind = %s, session_id = %s WHERE tenant_id = %s AND term = %s",
                                (heard_as, kind, session_id, self.tenant_id, term)).rowcount
        if updated:
            return False
        if count >= max_terms:
            raise EvaError(Code.INVALID_VALUE, "已确认的写法数量到达上限，先删掉不用的", max_terms=max_terms)
        self._execute("INSERT INTO tenant_terms (tenant_id, term, heard_as, kind, session_id, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                      (self.tenant_id, term, heard_as, kind, session_id, clock.now()))
        return True

    def remove(self, term: str) -> bool:
        """删除一条确认过的写法，返回是否真的删了。"""
        return self._execute("DELETE FROM tenant_terms WHERE tenant_id = %s AND term = %s", (self.tenant_id, term)).rowcount > 0

    def all(self) -> List[dict]:
        rows = self._execute("SELECT term, heard_as, kind FROM tenant_terms WHERE tenant_id = %s ORDER BY term", (self.tenant_id,)).fetchall()
        return [{"term": t, "heard_as": h, "kind": k} for t, h, k in rows]
