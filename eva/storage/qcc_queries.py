"""qcc_queries 表：query_company 每次调用的结果留痕，只追加。"""
from typing import Dict

from eva.common import clock
from eva.contracts.vocab import QccOutcome, check
from eva.storage.repo import TenantRepo
from eva.storage.sessions import Sessions

# 同一维度查过几次时看哪次：拿到过结果（有记录、确认没有）的比没拿到的算数，同级取最后一次
_RANK = {QccOutcome.RECORDS: 2, QccOutcome.NONE: 1, QccOutcome.UNMATCHED: 0, QccOutcome.FAILED: 0}


class QccQueries(TenantRepo):
    def add(self, session_id: str, uscc: str, dimension: str, outcome: QccOutcome) -> None:
        """会话不属于本租户抛 E3002。"""
        Sessions(self.conn, self.tenant_id).require(session_id)
        self._execute(
            "INSERT INTO qcc_queries (tenant_id, session_id, uscc, dimension, outcome, queried_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (self.tenant_id, session_id, uscc, dimension, check(QccOutcome, outcome).value, clock.now()))

    def outcomes(self, session_id: str, uscc: str) -> Dict[str, QccOutcome]:
        """本会话里这家公司各维度的查询结果；没查过的维度不在里面。"""
        rows = self._execute(
            "SELECT dimension, outcome FROM qcc_queries WHERE tenant_id = %s AND session_id = %s AND uscc = %s ORDER BY query_id",
            (self.tenant_id, session_id, uscc)).fetchall()
        best: Dict[str, QccOutcome] = {}
        for dimension, outcome in rows:
            o = QccOutcome(outcome)
            if dimension not in best or _RANK[o] >= _RANK[best[dimension]]:
                best[dimension] = o
        return best
