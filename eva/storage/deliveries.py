"""file_deliveries 表：文件交付给用户的记录，只追加。列历史报告靠它，不靠文件名猜。"""
from typing import List, Optional

from eva.common import clock, ids
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import FileStatus
from eva.storage.repo import TenantRepo

_NAMES = ("file_id", "filename", "bytes", "scenario", "uscc", "delivered_at")


def _like(text: str) -> str:
    """文件名关键字当普通文字查：通配符号要转义，否则用户输入 % 会匹配到所有文件。"""
    for ch in ("\\", "%", "_", "["):
        text = text.replace(ch, "\\" + ch)
    return f"%{text}%"


class Deliveries(TenantRepo):
    def add(self, file_id: str, session_id: str, scenario: Optional[str], uscc: Optional[str]) -> None:
        """登记一次交付。文件不属于本租户或不存在报 E2001，判断和写入在同一条语句里，查不到别人的文件。"""
        cur = self._execute(
            "INSERT INTO file_deliveries (delivery_id, tenant_id, file_id, session_id, scenario, uscc, delivered_at) "
            "SELECT %s, tenant_id, file_id, %s, %s, %s, %s FROM file_registry WHERE tenant_id = %s AND file_id = %s",
            (ids.new_id(), session_id, scenario, uscc, clock.now(), self.tenant_id, file_id))
        if cur.rowcount != 1:
            raise EvaError(Code.FILE_NOT_FOUND, "文件不存在", file_id=file_id)

    def recent(self, limit: int, name_like: Optional[str] = None, uscc: Optional[str] = None) -> List[dict]:
        """本租户交付过的可用文件，每份一行，场景和信用代码取最近一次交付的，按最近交付时间倒序。没有就返回空列表。
        uscc 只匹配交付时声明过它的文件。"""
        where, params = "", [self.tenant_id, FileStatus.AVAILABLE.value]
        if name_like:
            where += " AND f.filename LIKE %s ESCAPE '\\'"
            params.append(_like(name_like))
        if uscc:
            where += " AND d.uscc = %s"
            params.append(uscc)
        rows = self._execute(
            "SELECT TOP (%s) file_id, filename, bytes, scenario, uscc, delivered_at FROM ("
            " SELECT f.file_id, f.filename, f.bytes, d.scenario, d.uscc, d.delivered_at,"
            "  ROW_NUMBER() OVER (PARTITION BY f.file_id ORDER BY d.delivered_at DESC, d.delivery_id DESC) AS rn"
            " FROM file_deliveries d JOIN file_registry f ON f.tenant_id = d.tenant_id AND f.file_id = d.file_id"
            f" WHERE d.tenant_id = %s AND f.status = %s{where}) x WHERE rn = 1 ORDER BY delivered_at DESC",
            (limit, *params)).fetchall()
        return [dict(zip(_NAMES, r)) for r in rows]
