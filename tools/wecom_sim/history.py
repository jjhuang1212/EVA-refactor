"""模拟器重启后，从渠道消息表还原页面上的对话（只读）。

还原的是渠道实际收发的消息，文件气泡也在。回复下的统计行只存在于内存，还原不了。
"""
from functools import lru_cache
from typing import List, Tuple

from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import Channel, MessageDirection, MessageKind, TenantRealm
from eva.storage import db
from eva.storage.channel_messages import ChannelMessages
from eva.storage.identities import Identities
from eva.storage.objects import ObjectStore, Objects
from eva.tools.config import load_deliver_file

LIMIT = 500


def _item(r: dict) -> dict:
    role = "user" if r["direction"] == MessageDirection.IN.value else "bot"
    # 没有 file_id 又带全文的文件是长回复转成的 md，内容只存在消息里，还原成文字气泡
    if r["kind"] == MessageKind.TEXT.value or (r["file_id"] is None and r["text"] is not None):
        return {"role": role, "type": "text", "text": r["text"]}
    return {"role": role, "type": r["kind"], "filename": r["filename"] or "（无文件名）", "bytes": r["bytes"],
            "file_id": r["file_id"]}


def load(conn, tenant_id: str) -> List[dict]:
    """该租户最近 LIMIT 条渠道消息，按先后排列。没有消息返回空列表。"""
    return [_item(r) for r in ChannelMessages(conn, tenant_id).recent(LIMIT)]


def read(kf: str, user: str) -> List[dict]:
    """这个（客服号, 用户）的对话。用户还没发过消息（没有身份）返回空列表，不为了读历史建租户。"""
    with db.transaction() as conn:
        tenant_id = Identities(conn, TenantRealm.SIM.value).find(Channel.WECOM.value, kf, user)
        return [] if tenant_id is None else load(conn, tenant_id)


@lru_cache(maxsize=1)
def _store() -> ObjectStore:
    return ObjectStore.from_config()


def read_file(kf: str, user: str, file_id: str) -> Tuple[bytes, str, str]:
    """还原出来的文件气泡的下载：只能取这个用户租户自己的文件，返回（内容, 类型, 文件名）。"""
    with db.transaction() as conn:
        tenant_id = Identities(conn, TenantRealm.SIM.value).find(Channel.WECOM.value, kf, user)
        if tenant_id is None:
            raise EvaError(Code.FILE_NOT_FOUND, "文件不存在", file_id=file_id)
        row, data = Objects(conn, tenant_id, _store()).read_original(file_id, load_deliver_file().max_file_bytes)
    return data, row["mime"], row["filename"]


def known() -> List[dict]:
    """库里所有模拟用户，最近建的在前。模拟器重启后左侧列表靠它还原。"""
    with db.transaction() as conn:
        rows = Identities(conn, TenantRealm.SIM.value).list(Channel.WECOM.value)
    return [{"kf": r["scope"], "user": r["external_id"], "tenant": r["tenant_id"], "created_at": r["created_at"].isoformat()}
            for r in rows]
