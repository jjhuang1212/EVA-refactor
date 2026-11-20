"""channel_messages 表：渠道实际收到、发出的消息，只追加。只用来重建对话和排障，主流程不读它。"""
from typing import List

from eva.contracts.channel import ChannelMessage
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import MessageDirection, MessageKind, check
from eva.storage.repo import FK_VIOLATION, TenantRepo

MAX_MSG_ID = 128
_NAMES = ("seq", "direction", "msg_id", "kind", "text", "file_id", "filename", "bytes", "created_at")


class ChannelMessages(TenantRepo):
    def add(self, m: ChannelMessage) -> bool:
        """记一条消息，返回是否新增。同一方向的同一个 msg_id 已记过（重复投递、重发）返回 False，不报错。
        file_id 不属于本租户或不存在报 E2001，别的租户的文件登记进不来。"""
        direction, kind = check(MessageDirection, m.direction).value, check(MessageKind, m.kind).value
        if not isinstance(m.msg_id, str) or not m.msg_id.strip() or len(m.msg_id) > MAX_MSG_ID:
            raise EvaError(Code.INVALID_VALUE, f"msg_id 必须是不超过 {MAX_MSG_ID} 字符的非空字符串", field="msg_id")
        if m.file_id is not None and self._execute(
                "SELECT 1 FROM file_registry WHERE tenant_id = %s AND file_id = %s", (self.tenant_id, m.file_id)).fetchone() is None:
            raise EvaError(Code.FILE_NOT_FOUND, "文件不存在", file_id=m.file_id)
        cur = self._execute(
            "INSERT INTO channel_messages (tenant_id, direction, msg_id, kind, text, file_id, filename, bytes, created_at) "
            "SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s WHERE NOT EXISTS (SELECT 1 FROM channel_messages WITH (UPDLOCK, HOLDLOCK) "
            "WHERE tenant_id = %s AND direction = %s AND msg_id = %s)",
            (self.tenant_id, direction, m.msg_id, kind, m.text, m.file_id, m.filename, m.bytes, m.created_at,
             self.tenant_id, direction, m.msg_id),
            on_integrity={FK_VIOLATION: EvaError(Code.TENANT_NOT_FOUND, "租户不存在", tenant_id=self.tenant_id)})
        return cur.rowcount == 1

    def recent(self, limit: int) -> List[dict]:
        """本租户最近 limit 条消息，按先后升序。没有就返回空列表。"""
        cur = self._execute(
            f"SELECT {', '.join(_NAMES)} FROM (SELECT TOP (%s) {', '.join(_NAMES)} FROM channel_messages "
            "WHERE tenant_id = %s ORDER BY seq DESC) x ORDER BY seq", (limit, self.tenant_id))
        return [dict(zip(_NAMES, r)) for r in cur.fetchall()]
