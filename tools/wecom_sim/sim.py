"""模拟企微侧。

真实的 Worker 只通过 WecomClient 的六个方法（sync_msg、get_media、upload_media、send_text、send_menu、send_file）和企微打交道，
这里在这一层换成内存实现，其余（事件队列、游标、去重、窗口配额、核心、模型、存储）都是真的。
企微服务端对发送内容的限制（文本和菜单的字节数、菜单项数、素材大小）在这里照样拒收，Worker 超限时才暴露得出来。
"""
import logging
import mimetypes
import threading
import time
import uuid
from collections import Counter
from typing import Callable, Dict, List, Optional, Tuple

from eva.channels.wecom.config import WecomConfig
from eva.common import clock, logs
from eva.contracts.channel import Reply
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import EventType, TenantRealm
from eva.storage.identities import external_prefix

USER_PREFIX = external_prefix(TenantRealm.SIM)  # 模拟用户的渠道用户 ID 固定带它，页面展示、后缀由用户填或随机
FROM_CUSTOMER = 3  # sync_msg 里 origin=3 表示客户发来的消息
_MB = 1024 * 1024
_FEED_LIMIT = 500
log = logging.getLogger(__name__)


class Mailbox:
    """企微那一侧：用户发来的消息和素材、发给用户的消息，以及页面展示用的记录。"""

    def __init__(self, cfg: WecomConfig, notify: Callable[[str], None], resolve: Callable[[str, str], str]):
        """notify(kf) 在用户发来消息后调用，相当于企微回调把事件推进队列。resolve(客服号, 用户) 取租户编号，用户不合规时抛错。"""
        self._cfg, self._notify, self._resolve = cfg, notify, resolve
        self._tenants: Dict[Tuple[str, str], str] = {}
        self._lock = threading.Lock()
        self.run_id = uuid.uuid4().hex[:8]  # 游标带它：模拟器重启后收件箱清空，旧游标不能沿用
        self._inbox: Dict[str, List[dict]] = {}
        self._media: Dict[str, Tuple[bytes, str, Optional[str]]] = {}
        self._feed: Dict[Tuple[str, str], List[dict]] = {}
        self._seen_msgids: set = set()
        self._busy: Counter = Counter()
        self._who: Dict[str, Tuple[str, str]] = {}
        self._held: Dict[Tuple[str, str], dict] = {}
        self.alerts: List[dict] = []

    # ---- 用户一侧 ----

    def user_text(self, kf: str, user: str, text: str, menu_id: Optional[str] = None) -> None:
        """menu_id 非空表示点的是菜单：企微回调一条 text 消息，内容是菜单项文字，text.menu_id 是被点的菜单 ID。"""
        body = {"content": text, **({"menu_id": menu_id} if menu_id else {})}
        self._deliver(kf, user, {"msgtype": "text", "text": body}, {"type": "text", "text": text})

    def user_media(self, kf: str, user: str, kind: str, data: bytes, ctype: str, filename: str,
                   hide_name: bool, force_oversize: bool) -> None:
        """kind 为 file、image 或 voice。超限的处理按企微的行为：文件退化成只有文件名的文本，图片取回来不是图片，语音原样投递由渠道按大小拒收。"""
        limit = {"file": self._cfg.file_limit_mb, "image": self._cfg.image_limit_mb, "voice": self._cfg.voice_limit_mb}[kind] * _MB
        oversize = force_oversize or len(data) > limit
        shown = {"type": kind, "filename": filename, "bytes": len(data), "oversize": oversize, "hidden_name": hide_name}
        if oversize and kind == "file":
            self._deliver(kf, user, {"msgtype": "text", "text": {"content": filename}}, shown)
            return
        if oversize:
            data, ctype = "图片超过大小限制".encode("utf-8"), "text/plain"
        media_id = f"sim-{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._media[media_id] = (data, ctype, None if hide_name else filename)
        if kind == "image" and not oversize:
            shown["media_id"] = media_id
        self._deliver(kf, user, {"msgtype": kind, kind: {"media_id": media_id}}, shown)

    def _deliver(self, kf: str, user: str, body: dict, shown: dict) -> None:
        msg = {"msgid": uuid.uuid4().hex, "open_kfid": kf, "external_userid": user, "origin": FROM_CUSTOMER,
               "send_time": int(time.time()), **body}
        tenant = self._resolve(kf, user)
        with self._lock:
            self._inbox.setdefault(kf, []).append(msg)
            self._who[tenant] = (kf, user)
            self._tenants[(kf, user)] = tenant
        self.add(kf, user, {"role": "user", **shown})
        self._notify(kf)

    def media_bytes(self, media_id: str) -> Optional[Tuple[bytes, str]]:
        with self._lock:
            item = self._media.get(media_id)
        return (item[0], item[1]) if item else None

    def media_filename(self, media_id: str) -> Optional[str]:
        with self._lock:
            item = self._media.get(media_id)
        return item[2] if item else None

    # ---- WecomClient 要的三个方法的数据来源 ----

    def sync(self, kf: str, cursor: str) -> Tuple[List[dict], str]:
        run, _, idx = cursor.partition(":")
        start = int(idx) if run == self.run_id and idx.isdigit() else 0
        with self._lock:
            box = self._inbox.get(kf, [])
            return box[start:], f"{self.run_id}:{len(box)}"

    def get_media(self, media_id: str) -> Tuple[bytes, str, Optional[str]]:
        with self._lock:
            item = self._media.get(media_id)
        if item is None:
            raise EvaError(Code.CHANNEL_REJECTED, "素材不存在或已过期", media_id=media_id)
        return item

    def upload_media(self, data: bytes, filename: str) -> str:
        if len(data) > self._cfg.file_limit_mb * _MB:
            raise EvaError(Code.CHANNEL_REJECTED, "素材超过大小上限", bytes=len(data), limit_mb=self._cfg.file_limit_mb)
        media_id = f"sim-{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._media[media_id] = (data, mimetypes.guess_type(filename)[0] or "application/octet-stream", filename)
        return media_id

    def sent(self, user: str, kf: str, content: str, msgid: str) -> None:
        if len(content.encode("utf-8")) > self._cfg.text_max_bytes:
            raise EvaError(Code.CHANNEL_REJECTED, "文本超过单条字节上限", bytes=len(content.encode("utf-8")), limit=self._cfg.text_max_bytes)
        if self._first_time(msgid):
            self.add(kf, user, {"role": "bot", "type": "text", "text": content})
            self.flush_trace(kf, user)

    def sent_menu(self, user: str, kf: str, head: str, items: List[Tuple[str, str]], msgid: str) -> None:
        cfg = self._cfg
        if not 0 < len(head.encode("utf-8")) <= cfg.menu_head_max_bytes or not 0 < len(items) <= cfg.menu_max_items \
                or any(not 0 < len(label.encode("utf-8")) <= cfg.menu_item_max_bytes for _, label in items):
            raise EvaError(Code.CHANNEL_REJECTED, "菜单超出企微限制", head_bytes=len(head.encode("utf-8")), items=len(items),
                           limits=[cfg.menu_head_max_bytes, cfg.menu_max_items, cfg.menu_item_max_bytes])
        if self._first_time(msgid):
            self.add(kf, user, {"role": "bot", "type": "menu", "text": head, "items": [{"id": i, "label": t} for i, t in items]})
            self.flush_trace(kf, user)

    def sent_file(self, user: str, kf: str, media_id: str, msgid: str) -> None:
        with self._lock:
            item = self._media.get(media_id)
        if item is None:
            raise EvaError(Code.CHANNEL_REJECTED, "素材不存在或已过期", media_id=media_id)
        if self._first_time(msgid):
            self.add(kf, user, {"role": "bot", "type": "file", "filename": item[2], "bytes": len(item[0]), "media_id": media_id})
            self.flush_trace(kf, user)

    def _first_time(self, msgid: str) -> bool:
        with self._lock:  # 企微按 msgid 去重
            if msgid in self._seen_msgids:
                return False
            self._seen_msgids.add(msgid)
            return True

    def hold_trace(self, who: Tuple[str, str], item: dict) -> None:
        """统计要显示在回复下面，而核心先返回、渠道后发送，所以先压着，回复发出后再入账。"""
        with self._lock:
            self._held[who] = item

    def flush_trace(self, kf: str, user: str) -> None:
        with self._lock:
            item = self._held.pop((kf, user), None)
        if item:
            self.add(kf, user, item)

    # ---- 页面展示 ----

    def add(self, kf: str, user: str, item: dict) -> None:
        with self._lock:
            feed = self._feed.setdefault((kf, user), [])
            feed.append({**item, "seq": (feed[-1]["seq"] + 1) if feed else 1, "ts": time.time()})
            del feed[:-_FEED_LIMIT]

    def feed(self, kf: str, user: str, since: int) -> Tuple[List[dict], bool]:
        with self._lock:
            return [m for m in self._feed.get((kf, user), []) if m["seq"] > since], self._busy[(kf, user)] > 0

    def conversations(self) -> List[dict]:
        with self._lock:
            rows = [{"kf": kf, "user": user, "tenant": self._tenants.get((kf, user)), "count": len(f), "last_ts": f[-1]["ts"],
                     "seq": f[-1]["seq"], "last_role": f[-1]["role"], "preview": _preview(f[-1]), "busy": self._busy[(kf, user)] > 0}
                    for (kf, user), f in self._feed.items() if f]
        return sorted(rows, key=lambda r: -r["last_ts"])

    def alert(self, text: str) -> None:
        with self._lock:
            self.alerts.append({"seq": len(self.alerts) + 1, "ts": time.time(), "text": text})

    def alerts_since(self, since: int) -> List[dict]:
        with self._lock:
            return self.alerts[since:]

    def set_busy(self, tenant: str, delta: int) -> Optional[Tuple[str, str]]:
        with self._lock:
            who = self._who.get(tenant)
            if who:
                self._busy[who] += delta
            return who


def _preview(item: dict) -> str:
    return item.get("text") or f"[{'图片' if item.get('type') == 'image' else '文件'}] {item.get('filename', '')}"


class SimClient:
    """顶替 WecomClient，Worker 看到的接口与真实的一致。"""

    def __init__(self, box: Mailbox):
        self._box = box

    def sync_msg(self, kf_token: str, open_kfid: str, cursor: str):
        msgs, next_cursor = self._box.sync(open_kfid, cursor)
        return msgs, next_cursor, False

    def get_media(self, media_id: str):
        return self._box.get_media(media_id)

    def send_text(self, touser: str, open_kfid: str, content: str, msgid: str) -> str:
        self._box.sent(touser, open_kfid, content, msgid)
        return msgid

    def send_menu(self, touser: str, open_kfid: str, head: str, items: List[Tuple[str, str]], msgid: str) -> str:
        self._box.sent_menu(touser, open_kfid, head, items, msgid)
        return msgid

    def upload_media(self, data: bytes, filename: str) -> str:
        return self._box.upload_media(data, filename)

    def send_file(self, touser: str, open_kfid: str, media_id: str, msgid: str) -> str:
        self._box.sent_file(touser, open_kfid, media_id, msgid)
        return msgid


class TracingHub:
    """包住真实的 Hub：记下处理中的状态，回复附上这一轮的步数、费用、耗时和用到的工具。"""

    def __init__(self, hub, box: Mailbox):
        self._hub, self._box = hub, box

    def submit(self, tenant_id: str, text: str, msg_id: str, cancel: threading.Event, on_file=None) -> Optional[Reply]:
        return self._traced(tenant_id, lambda: self._hub.submit(tenant_id, text, msg_id, cancel, on_file))

    def voice(self, tenant_id: str, data: bytes, ext: str, msg_id: str, cancel: threading.Event, on_file=None) -> Optional[Reply]:
        return self._traced(tenant_id, lambda: self._hub.voice(tenant_id, data, ext, msg_id, cancel, on_file))

    def reset_topic(self, tenant_id: str) -> str:
        return self._hub.reset_topic(tenant_id)

    def _traced(self, tenant_id: str, run) -> Optional[Reply]:
        who, started = self._box.set_busy(tenant_id, 1), time.monotonic()
        if who:
            self._box.flush_trace(*who)  # 上一轮的统计若没等到回复（发送失败），先放出来，不能挂到这一轮的回复下
        try:
            reply = run()
        finally:
            self._box.set_busy(tenant_id, -1)
        if reply is not None and who:
            tools = Counter(e["payload"]["name"] for e in reply.events if e["event_type"] == EventType.TOOL_CALL.value)
            self._box.hold_trace(who, {"role": "trace", "steps": reply.steps, "cost": str(reply.cost_cny), "reason": reply.reason,
                                 "seconds": round(time.monotonic() - started, 1), "tools": dict(tools),
                                 "model_seconds": [e["payload"].get("seconds") for e in reply.events if e["event_type"] == EventType.ASSISTANT_MESSAGE.value]})
        return reply

    def upload(self, tenant_id: str, filename: str, data: bytes, msg_id: str) -> Optional[dict]:
        self._box.set_busy(tenant_id, 1)
        try:
            return self._hub.upload(tenant_id, filename, data, msg_id)
        finally:
            self._box.set_busy(tenant_id, -1)


def report_to(box: Mailbox):
    def report(error: EvaError, context: dict) -> None:
        logs.report_error(log, error, **context)
        box.alert(f"{error.code.value} {error.code.name}：{error.message} {error.details} {context} @ {clock.now():%H:%M:%S}")
    return report
