"""企微消息处理：取事件、拉消息、交给核心、把回复发回去。分成拉取和处理两段，互不等待。

拉取：一个事件对应一次 sync_msg 分页，把客户消息逐条写进消息队列，一页都入队后才保存游标、确认事件。
中途失败（可重试的错误）不保存游标也不确认，事件闲置后被重新认领，整页重拉；
重复入队的消息靠 msgid 在核心侧去重，不会处理两遍。拉取很快，不会被某个用户的长任务拖住。
回调只是提示，丢了就没人触发拉取，所以启动时和之后每隔一段时间，还会不带 token 按游标对账一遍。

处理：从消息队列取出，按租户分道，同一租户一条一条按顺序处理，不同租户并行（上限见配置）。
处理完才确认。单条消息的不可重试失败（比如模型拒绝了请求）只影响这一条：上报、尽量告诉用户，然后继续下一条。
可重试的失败不确认，该租户后面的消息也先不处理，等这条闲置后被重新认领，仍按原顺序。
"""
import base64
import hashlib
import json
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from eva.channels.wecom.client import WecomClient
from eva.channels.wecom.config import WecomConfig
from eva.channels.wecom.lanes import Lanes
from eva.channels.wecom.ports import Hub, Record, Resolve, State
from eva.common import choices, clock, logs
from eva.contracts.channel import CANCEL_MENU_ID, ChannelMessage, Reply, ReplyFile
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import MessageDirection, MessageKind, TurnEnd

_FROM_CUSTOMER = 3  # sync_msg 的 origin：客户发来的消息；其余是系统事件和客服人员消息
UNSUPPORTED = "暂不支持这类消息（视频、位置等），请发文字、语音、图片或文件。"
VOICE_OVERSIZE = "语音没有收到，可能超过了企微的大小限制（不超过 {mb}MB）。请分段重发，或直接发文字。"
VOICE_EXTENSION = ".amr"  # 企微语音素材默认 amr（sync_msg 的 voice_format 缺省）
OVERSIZE = "文件或图片没有收到，可能超过了企微的大小限制（文件不超过 {file_mb}MB，图片不超过 {image_mb}MB）。请压缩或拆分后重发。"
OVERFLOW = "内容较长（{chars} 字），已整理成文件，请查收。"
FILE_TOO_LARGE = "文件《{name}》超过企微 {mb}MB 的限制，没有发出。"
CONTINUE = "1"  # 用户回这个，就是要接收被配额截下的内容
HINT = "（受企微限制，一次最多发 {n} 条，后面还有内容。回复 {key} 继续接收。）"
BUSY_HEAD = "还在处理中，需要的话可以取消。"
CANCEL_ITEM = "取消任务"
CANCELLING = "收到，正在停止。"
CANCELLED = "已停止，已完成的部分保留。想接着做，直接回复我就行。"
NO_RUN = "当前没有进行中的任务。"
QUOTA_EXHAUSTED = "今天的使用额度已经用完，将于 {when} 重置，之后可以继续。"
_MB = 1024 * 1024
_IMAGE_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png"}  # 企微图片素材的类型
_CHANNEL_CODES = (Code.CHANNEL_UNAVAILABLE, Code.CHANNEL_REJECTED, Code.DELIVERY_LIMITED)

Report = Callable[[EvaError, dict], None]
log = logging.getLogger(__name__)


def _entry_order(entry_id: str) -> Tuple[int, ...]:
    """Stream 条目 ID（毫秒-序号）的先后顺序。"""
    return tuple(int(x) for x in entry_id.split("-"))


def split_text(text: str, max_bytes: int) -> List[str]:
    """按 UTF-8 字节数切成多段，不拆字符；尽量在换行处断开。拼起来等于原文（纯空白的段除外，企微不收）。"""
    parts, rest = [], text
    while len(rest.encode("utf-8")) > max_bytes:
        cut = rest.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
        nl = cut.rfind("\n", len(cut) // 2)
        if nl > 0:
            cut = cut[:nl + 1]
        parts.append(cut)
        rest = rest[len(cut):]
    parts.append(rest)
    return [p for p in parts if p.strip()]


class _Cancel:
    """一次运行的取消信号。核心只调 is_set()：用户点了取消，或进程在停机，都算取消。"""

    def __init__(self, stop: threading.Event):
        self._stop, self._own = stop, threading.Event()
        self.started = int(clock.unix())  # 取消点击要不早于这一轮开始才算数；企微的发送时间只到秒

    def set(self) -> None:
        self._own.set()

    def is_set(self) -> bool:
        return self._own.is_set() or self._stop.is_set()


class Worker:
    def __init__(self, cfg: WecomConfig, client: WecomClient, state: State, hub: Hub, report: Report,
                 stop: Optional[threading.Event] = None, *, resolve: Resolve, record: Record):
        self._cfg, self._client, self._state, self._hub, self._report = cfg, client, state, hub, report
        self._resolve = resolve  # (客服号, 外部联系人) -> 租户编号；租户怎么来的不归渠道管
        self._record = record
        self._stop = stop or threading.Event()
        self._guard = threading.Lock()
        self._known = set()  # 已取出、还在内存里排队或处理的消息队列条目，重新认领时靠它跳过
        self._runs: Dict[str, _Cancel] = {}  # 租户 -> 正在跑的那一轮的取消信号；取消点击靠它找到目标
        self._held: Dict[str, str] = {}  # 租户 -> 可重试失败的那条的 ID；它之后的消息等它重试
        self._lanes = Lanes(cfg.max_parallel_tenants, self._process, self._dropped)
        self._dispatch_error: Optional[BaseException] = None

    def run_forever(self, consumer: str) -> None:
        """拉取在调用线程，派发在另一个线程，处理在线程池。任何一边出了未预期的异常，这里原样抛出。"""
        dispatcher = threading.Thread(target=self._dispatch_forever, args=(consumer,), name="dispatch", daemon=True)
        dispatcher.start()
        due = 0.0  # 启动时立刻对账一次：宕机期间回调没收到，不会有事件触发拉取
        try:
            while not self._stop.is_set():
                self._raise_fatal()
                try:
                    if time.monotonic() >= due:
                        due = time.monotonic() + self._cfg.reconcile_seconds
                        self.reconcile()
                    self.pull_once(consumer)
                except EvaError as e:  # 队列或 Redis 层的错误：上报并稍等，不空转
                    self._report(e, {"where": "queue"})
                    self._stop.wait(1)
            self._raise_fatal()
        finally:
            self._stop.set()
            dispatcher.join()
            self._lanes.close()

    def run_once(self, consumer: str) -> int:
        """拉一批事件，取出队列里的消息，处理到全部完成。返回处理的事件数。"""
        n = self.pull_once(consumer)
        while self.dispatch_once(consumer) >= self._cfg.reserve_count:
            pass
        self._lanes.wait_idle()
        self._raise_fatal()
        return n

    def _raise_fatal(self) -> None:
        err = self._lanes.fatal or self._dispatch_error
        if err is not None:
            raise err

    # ---- 拉取：事件 -> 消息队列 ----

    def pull_once(self, consumer: str) -> int:
        """取一批事件，把其中的消息入队，返回处理的事件数。"""
        entries = self._state.reserve(consumer, self._cfg.reserve_count, self._cfg.block_ms, self._cfg.claim_idle_ms)
        for entry_id, fields in entries:
            try:
                finished = self._pull(fields["kf_token"], fields["open_kfid"])
            except EvaError as e:
                self._report(e, {"where": "event", "entry_id": entry_id})
                if e.retryable:
                    continue  # 不确认：闲置后重新认领
                finished = True
            if finished:
                self._state.ack(entry_id)
        return len(entries)

    def reconcile(self) -> None:
        """回调丢了或服务停过，消息还在企微那边。对每个有游标的客服号不带 token 补拉一遍；
        从保存的游标往后拉，拉过的不会重复。某个号失败只上报，不影响其他号，下个间隔再试。"""
        for kf in self._state.cursor_kfids():
            try:
                self._pull("", kf)
            except EvaError as e:
                self._report(e, {"where": "reconcile", "open_kfid": kf})

    def _pull(self, token: str, open_kfid: str) -> bool:
        """入队完返回 True；中途收到停止信号返回 False（不保存游标、不确认，下次启动接着处理）。"""
        cursor = self._state.get_cursor(open_kfid)
        while True:
            msgs, next_cursor, more = self._client.sync_msg(token, open_kfid, cursor)
            sent = [m["send_time"] for m in msgs if m.get("origin") == _FROM_CUSTOMER and isinstance(m.get("send_time"), int)]
            if sent and not token:  # 对账拉到了客户消息：回调漏过，或事件还没轮到处理
                logs.emit(log, logging.INFO, "对账拉到客户消息", open_kfid=open_kfid, count=len(sent),
                          oldest_age_seconds=int(clock.unix()) - min(sent))
            for msg in msgs:
                if self._stop.is_set():
                    return False
                self._enqueue(msg)
            self._state.save_cursor(open_kfid, next_cursor)
            cursor = next_cursor
            if not more:
                return True

    def _enqueue(self, msg: dict) -> None:
        if msg.get("origin") != _FROM_CUSTOMER:
            logs.emit(log, logging.DEBUG, "跳过非客户消息", origin=msg.get("origin"))
            return
        try:
            msgid, kf, user, sent_at = self._identify(msg)
        except EvaError as e:  # 坏消息分不出租户，只能上报并跳过，不卡住后面的
            self._report(e, {"where": "message", "msgid": msg.get("msgid")})
            return
        try:
            tenant = self._resolve(kf, user)
        except EvaError as e:
            if e.retryable:
                raise
            self._report(e, {"where": "message", "msgid": msgid})  # 渠道用户 ID 不合规的消息分不出租户，跳过，不卡住这一页的其余消息
            return
        if msg.get("msgtype") == "text" and (msg.get("text") or {}).get("menu_id") == CANCEL_MENU_ID:
            self._cancel(tenant, kf, user, msgid, sent_at)  # 排进租户的通道就要等运行结束，所以在拉取线程里直接办
            return
        self._state.enqueue({"tenant": tenant, "msg": json.dumps(msg, ensure_ascii=False)})

    def _cancel(self, tenant: str, kf: str, user: str, msgid: str, sent_at: int) -> None:
        """点击不刷新下发窗口和配额（企微没说点击算不算用户主动发消息，按不算处理，见 docs/待决问题.md）。
        没有在途运行，或点击早于当前这一轮的开始（过期的按钮，它要取消的那轮早结束了），
        如实告知，不算错误，也不能误停后面的运行。"""
        try:
            with self._guard:
                run = self._runs.get(tenant)
            if run is None or sent_at < run.started:
                self._send(kf, user, NO_RUN, msgid, "cancel_none")
                return
            run.set()
            self._send(kf, user, CANCELLING, msgid, "cancelling")
        except EvaError as e:  # 取消信号已发出或无需发出，回复失败只上报，不能卡住这一页的其余消息
            self._report(e, {"where": "cancel", "msgid": msgid})

    @contextmanager
    def _running(self, tenant: str, kf: str, user: str, msgid: str) -> Iterator[_Cancel]:
        cancel = _Cancel(self._stop)
        with self._guard:
            self._runs[tenant] = cancel
        timer = threading.Timer(self._cfg.busy_notice_seconds, self._offer_cancel, (tenant, kf, user, msgid, cancel))
        timer.daemon = True
        timer.start()
        try:
            yield cancel
        finally:
            timer.cancel()
            with self._guard:
                del self._runs[tenant]

    def _offer_cancel(self, tenant: str, kf: str, user: str, msgid: str, cancel: _Cancel) -> None:
        """运行超时还没结束，下发取消入口。要给“正在停止”和最终回复留额度，留不出就不发并上报。"""
        try:
            with self._guard:
                if self._runs.get(tenant) is not cancel:
                    return
            left = self._state.delivery_left(kf, user, clock.unix(), self._window_seconds, self._cfg.quota_per_window)
            if left is None or left <= self._cfg.reserve_for_final + 1:
                raise EvaError(Code.DELIVERY_LIMITED, "额度不够，取消入口未发出",
                               reason="window" if left is None else "quota", left=left, quota=self._cfg.quota_per_window)
            why = self._state.reserve_delivery(kf, user, 1, clock.unix(), self._window_seconds, self._cfg.quota_per_window)
            if why is not None:
                raise EvaError(Code.DELIVERY_LIMITED, "额度不够，取消入口未发出", reason=why, quota=self._cfg.quota_per_window)
            base = hashlib.sha1(f"{msgid}:cancel_offer".encode("utf-8")).hexdigest()[:24]
            self._client.send_menu(user, kf, BUSY_HEAD, [(CANCEL_MENU_ID, CANCEL_ITEM)], base)
        except EvaError as e:  # 在定时线程里没有调用方可抛，上报
            self._report(e, {"where": "cancel_offer", "msgid": msgid})

    @staticmethod
    def _identify(msg: dict) -> Tuple[str, str, str, int]:
        msgid, kf, user, sent_at = (msg.get(k) for k in ("msgid", "open_kfid", "external_userid", "send_time"))
        bad = [k for k, v in (("msgid", msgid), ("open_kfid", kf), ("external_userid", user)) if not isinstance(v, str) or not v]
        if bad or not isinstance(sent_at, int):
            raise EvaError(Code.CHANNEL_REJECTED, "企微消息缺少必要字段", missing=bad + ([] if isinstance(sent_at, int) else ["send_time"]))
        return msgid, kf, user, sent_at

    # ---- 派发：消息队列 -> 按租户分道 ----

    def _dispatch_forever(self, consumer: str) -> None:
        try:
            while not self._stop.is_set() and self._lanes.fatal is None:
                try:
                    self.dispatch_once(consumer)
                except EvaError as e:
                    self._report(e, {"where": "queue"})
                    self._stop.wait(1)
        except BaseException as e:  # noqa: BLE001 留给 run_forever 原样抛出
            self._dispatch_error = e

    def dispatch_once(self, consumer: str) -> int:
        """取一批消息交给各租户的道，返回取到的条数。内存里排队的消息满了就先不取。"""
        room = self._cfg.max_queued_messages - self._lanes.pending()
        if room <= 0:
            self._stop.wait(0.05)
            return 0
        entries = self._state.reserve_messages(consumer, min(room, self._cfg.reserve_count), self._cfg.block_ms,
                                               self._cfg.claim_idle_ms)
        for entry_id, fields in entries:
            self._admit(entry_id, fields)
        return len(entries)

    def _admit(self, entry_id: str, fields: Dict[str, str]) -> None:
        tenant = fields["tenant"]
        with self._guard:
            if entry_id in self._known:
                return  # 重新认领到了还在排队或处理中的
            floor = self._held.get(tenant)
            if floor is not None:
                if _entry_order(entry_id) > _entry_order(floor):
                    return  # 前面有失败待重试的消息，后面的等它，保持顺序
                del self._held[tenant]
            self._known.add(entry_id)
        self._lanes.add(tenant, (entry_id, fields))

    def _dropped(self, tenant: str, rest: List[Tuple[str, Dict[str, str]]]) -> None:
        """这一道放弃了没处理的消息：它们没确认，留给重新认领。"""
        with self._guard:
            self._known.difference_update(entry_id for entry_id, _ in rest)

    # ---- 处理：一条消息 ----

    def _process(self, tenant: str, entry: Tuple[str, Dict[str, str]]) -> bool:
        """处理并确认一条消息。返回 False 表示本道后面的先不处理（停止，或这条要等重试）。"""
        entry_id, fields = entry
        if self._stop.is_set():
            with self._guard:
                self._known.discard(entry_id)
            return False
        msg = json.loads(fields["msg"])
        try:
            self._handle(msg, tenant)
        except EvaError as e:  # 能到这里的都是可重试的
            self._report(e, {"where": "message", "entry_id": entry_id, "msgid": msg.get("msgid")})
            with self._guard:
                self._known.discard(entry_id)
                self._held[tenant] = entry_id
            return False
        try:
            self._state.ack_message(entry_id)
        except EvaError as e:  # 已经处理过了，没确认只会被重新认领后靠 msgid 去重
            self._report(e, {"where": "queue", "entry_id": entry_id})
        with self._guard:
            self._known.discard(entry_id)
        return True

    def _handle(self, msg: dict, tenant: str) -> None:
        try:
            self._dispatch(msg, tenant)
        except EvaError as e:
            if e.retryable:
                raise
            self._report(e, {"where": "message", "msgid": msg.get("msgid")})
            if e.code not in _CHANNEL_CODES:  # 渠道本身出了问题时，再通过它通知用户没有意义
                self._notify_failure(msg, e)

    def _dispatch(self, msg: dict, tenant: str) -> None:
        msgid, kf, user, sent_at = self._identify(msg)
        if not (msg.get("text") or {}).get("menu_id"):  # 菜单点击不刷新窗口和配额，见 _cancel
            self._state.note_inbound(kf, user, sent_at, self._window_seconds)
        with logs.bind(tenant=tenant, msg_id=msgid[:8]):
            text = (msg.get("text") or {}).get("content") if msg.get("msgtype") == "text" else None
            if isinstance(text, str) and text.strip():
                self._note_in(tenant, msgid, MessageKind.TEXT, sent_at, text=text)
            flushed = self._flush_held(kf, user, msgid)
            if flushed and msg.get("msgtype") == "text" and (msg.get("text") or {}).get("content", "").strip() == CONTINUE:
                return  # 这条只是“继续”的口令，不用交给核心
            if msg.get("msgtype") == "text" and isinstance(text, str) and text.strip() == self._cfg.reset_command:
                self._send(kf, user, self._hub.reset_topic(tenant), msgid, "reset")
                return
            self._route(msg, tenant, kf, user, msgid, sent_at)

    def _route(self, msg: dict, tenant: str, kf: str, user: str, msgid: str, sent_at: int) -> None:
        kind = msg.get("msgtype")
        if kind == "text":
            text = (msg.get("text") or {}).get("content")
            if not isinstance(text, str) or not text.strip():
                raise EvaError(Code.CHANNEL_REJECTED, "文本消息没有内容", msgid=msgid)
            logs.emit(log, logging.INFO, "收到文本", chars=len(text), preview=logs.preview(text))
            if self._is_oversize_file_notice(text):
                self._send(kf, user, self._oversize_notice(), msgid, "rejected")
                return
            with self._running(tenant, kf, user, msgid) as cancel:
                reply = self._hub.submit(tenant, text, msgid, cancel, self._deliver_now(kf, user, msgid))
            if reply is None:
                logs.emit(log, logging.INFO, "重复消息，已处理过，跳过")
            else:
                self._send_reply(kf, user, reply, msgid)
        elif kind in ("file", "image"):
            media_id = (msg.get(kind) or {}).get("media_id")
            if not isinstance(media_id, str) or not media_id:
                raise EvaError(Code.CHANNEL_REJECTED, "文件或图片消息没有 media_id", msgid=msgid, msgtype=kind)
            data, ctype, name = self._client.get_media(media_id)
            logs.emit(log, logging.INFO, "收到图片" if kind == "image" else "收到文件", bytes=len(data), type=ctype, name=name)
            if kind == "image":
                ext = _IMAGE_EXTENSIONS.get(ctype.split(";")[0].strip().lower())
                if ext is None:  # 超出大小限制时企微返回的是一段文字提示，不是图片
                    self._note_in(tenant, msgid, MessageKind.IMAGE, sent_at)
                    self._send(kf, user, self._oversize_notice(), msgid, "rejected")
                    return
                name = name or f"图片_{sent_at}{ext}"
                name = name if name.lower().endswith((".jpg", ".jpeg", ".png")) else name + ext
            else:
                name = name or f"未命名文件_{sent_at}"  # 企微没给文件名时的命名，是渠道的事，核心只认非空文件名
            info = self._hub.upload(tenant, name, data, msgid)
            if info is None:
                logs.emit(log, logging.INFO, "重复上传，已处理过，跳过")
            else:  # 回复里用登记后的名字，同名不同内容时它和用户发来的不同
                self._note_in(tenant, msgid, MessageKind(kind), sent_at,
                              file_id=info["file_id"], filename=info["filename"], bytes=len(data))
                self._send(kf, user, f"已收到{'图片' if kind == 'image' else '文件'}：{info['filename']}", msgid, "ack")
        elif kind == "voice":
            media_id = (msg.get("voice") or {}).get("media_id")
            if not isinstance(media_id, str) or not media_id:
                raise EvaError(Code.CHANNEL_REJECTED, "语音消息没有 media_id", msgid=msgid)
            data, ctype, _ = self._client.get_media(media_id)
            logs.emit(log, logging.INFO, "收到语音", bytes=len(data), type=ctype)
            oversize = ctype.lower().startswith("text/")  # 超出大小限制时企微返回的是一段文字提示，不是语音
            self._note_in(tenant, msgid, MessageKind.VOICE, sent_at, bytes=None if oversize else len(data))
            if oversize:
                self._send(kf, user, VOICE_OVERSIZE.format(mb=self._cfg.voice_limit_mb), msgid, "rejected")
                return
            with self._running(tenant, kf, user, msgid) as cancel:
                reply = self._hub.voice(tenant, data, VOICE_EXTENSION, msgid, cancel, self._deliver_now(kf, user, msgid))
            if reply is None:
                logs.emit(log, logging.INFO, "重复消息，已处理过，跳过")
            else:
                self._send_reply(kf, user, reply, msgid)
        else:
            logs.emit(log, logging.INFO, "不支持的消息类型", msgtype=kind)
            self._send(kf, user, UNSUPPORTED, msgid, "unsupported")

    def _deliver_now(self, kf: str, user: str, msgid: str):
        """turn 中途要先发的文件（如原始转录）：走和最终回复同一套切段、窗口、配额检查。同一条入站消息里每个文件的 msgid 各不相同。"""
        def send(f: ReplyFile) -> bool:
            """配额不够给最终回复留位置时返回 False，文件改为随最终回复发出。"""
            return self._send(kf, user, "", msgid, "file:" + f.filename, [f], keep=self._cfg.reserve_for_final)
        return send

    def _oversize_notice(self) -> str:
        return OVERSIZE.format(file_mb=self._cfg.file_limit_mb, image_mb=self._cfg.image_limit_mb)

    def _is_oversize_file_notice(self, text: str) -> bool:
        name = text.strip()
        stem, dot, ext = name.rpartition(".")
        return bool(dot and stem and ext.lower() in self._cfg.file_extensions and not any(c.isspace() or c in "/\\" for c in name))

    @property
    def _window_seconds(self) -> float:
        return self._cfg.window_hours * 3600

    def _send(self, kf: str, user: str, text: str, inbound_msgid: str, kind: str,
              files: Sequence[ReplyFile] = (), keep: int = 0) -> bool:
        """切段、核对窗口与配额、再发。返回是否发了。窗口已过抛 E7004。
        配额不够整条回复发完时，发得下的先发，最后一段文字后追加“回复 1 继续”，发不下的存起来，用户回复后补发。
        keep>0（中途的文件）时配额不够给最终回复留 keep 条，就什么都不发，返回 False。
        文本切出的段数超过 max_text_parts 时，改成一条短提示加一个 md 文件，不让长文占满配额。"""
        limit = self._cfg.file_limit_mb * _MB
        for f in [f for f in files if f.inline_if_short]:  # 够短的文本文件直接当文字发，放不下的仍发文件
            merged = "\n\n".join(x for x in (text, f.data.decode("utf-8")) if x)
            if len(split_text(merged, self._cfg.text_max_bytes)) <= self._cfg.max_text_parts:
                text, files = merged, [x for x in files if x is not f]
        big, files = [f for f in files if len(f.data) > limit], [f for f in files if len(f.data) <= limit]
        for f in big:  # 发不出去的文件要让用户知道，也要上报，不能悄悄丢掉
            self._report(EvaError(Code.CHANNEL_REJECTED, "文件超过企微大小限制，没有发出", filename=f.filename,
                                  bytes=len(f.data), limit_mb=self._cfg.file_limit_mb), {"where": "deliver", "msgid": inbound_msgid})
        if big:
            text = "\n".join([text, *(FILE_TOO_LARGE.format(name=f.filename, mb=self._cfg.file_limit_mb) for f in big)])
        parts = split_text(text, self._cfg.text_max_bytes)
        if len(parts) > self._cfg.max_text_parts:
            files.append(ReplyFile(f"回复内容-{clock.now():%Y%m%d-%H%M%S}.md", "text/markdown", text.encode("utf-8")))
            parts = [OVERFLOW.format(chars=len(text))]
        left = self._state.delivery_left(kf, user, clock.unix(), self._window_seconds, self._cfg.quota_per_window)
        if left is None:
            raise EvaError(Code.DELIVERY_LIMITED, "超出企微下发窗口，回复未发出", reason="window", parts=len(parts), files=len(files),
                           quota=self._cfg.quota_per_window)
        need = len(parts) + len(files)
        if keep and need + keep > left:
            return False
        if need > left:
            parts, files, rest = self._cut(parts, files, left)
            self._state.hold(kf, user, _dump(*rest), self._window_seconds)
            self._report(EvaError(Code.DELIVERY_LIMITED, "超出企微条数配额，放不下的部分已存起来，等用户回复后补发",
                                  reason="quota", sent=len(parts) + len(files), held_files=[f.filename for f in rest[1]],
                                  quota=self._cfg.quota_per_window), {"where": "deliver", "msgid": inbound_msgid})
        media = [self._client.upload_media(f.data, f.filename) for f in files]  # 上传不占配额，先传，失败时什么都没发
        why = self._state.reserve_delivery(kf, user, len(parts) + len(media), clock.unix(), self._window_seconds,
                                           self._cfg.quota_per_window)
        if why is not None:
            raise EvaError(Code.DELIVERY_LIMITED, "超出企微下发窗口或条数配额，回复未发出",
                           reason=why, parts=len(parts), files=len(media), quota=self._cfg.quota_per_window)
        logs.emit(log, logging.INFO, "发送回复", kind=kind, parts=len(parts), files=[f.filename for f in files],
                  bytes=len(text.encode("utf-8")), preview=logs.preview(text))
        base = hashlib.sha1(f"{inbound_msgid}:{kind}".encode("utf-8")).hexdigest()[:24]
        tenant = self._resolve(kf, user)
        for i, part in enumerate(parts):  # 确定性的 msgid：企微侧也能识别同一条回复的重发
            self._client.send_text(user, kf, part, f"{base}-{i}")
            self._note(tenant, f"{base}-{i}", MessageDirection.OUT, MessageKind.TEXT, text=part)
        for i, (media_id, f) in enumerate(zip(media, files)):
            self._client.send_file(user, kf, media_id, f"{base}-f{i}")
            # 没有 file_id 的是长回复转成的 md，内容不在文件登记里，记下全文才能重建
            self._note(tenant, f"{base}-f{i}", MessageDirection.OUT, MessageKind.FILE, file_id=f.file_id, filename=f.filename,
                       bytes=len(f.data), text=f.data.decode("utf-8", "replace") if f.file_id is None else None)
        return True

    def _note_in(self, tenant: str, msgid: str, kind: MessageKind, sent_at: int, **fields) -> None:
        self._note(tenant, msgid, MessageDirection.IN, kind, clock.from_unix(sent_at), **fields)

    def _note(self, tenant: str, msg_id: str, direction: MessageDirection, kind: MessageKind,
              created_at=None, **fields) -> None:
        """渠道已经收到或发出的消息记一笔。记失败时消息已经收发，不能回滚也不能重试（会重复发），只上报。"""
        try:
            self._record(tenant, ChannelMessage(direction, msg_id, kind, created_at or clock.now(), **fields))
        except EvaError as e:
            self._report(e, {"where": "record", "msgid": msg_id})

    def _send_reply(self, kf: str, user: str, reply: Reply, inbound_msgid: str) -> bool:
        """带选项的回复优先发成菜单。放不进菜单（选项太多、问题太长、同时还有文件）或额度不够再发菜单，就改发编号文字，
        文字走 _send，额度不够时的"回复 1 继续"和存起来补发都照旧。"""
        text = _reply_text(reply)
        if not reply.options:
            return self._send(kf, user, text, inbound_msgid, "reply", reply.files)
        if (len(reply.options) <= self._cfg.menu_max_items and not reply.files
                and 0 < len(text.encode("utf-8")) <= self._cfg.menu_head_max_bytes
                and self._state.reserve_delivery(kf, user, 1, clock.unix(), self._window_seconds, self._cfg.quota_per_window) is None):
            items = [(f"pick_{i}", _clip(choices.label(i, o), self._cfg.menu_item_max_bytes)) for i, o in enumerate(reply.options, 1)]
            base = hashlib.sha1(f"{inbound_msgid}:menu".encode("utf-8")).hexdigest()[:24]
            logs.emit(log, logging.INFO, "发送选项菜单", options=len(items), preview=logs.preview(text))
            self._client.send_menu(user, kf, text, items, base)
            self._note(self._resolve(kf, user), base, MessageDirection.OUT, MessageKind.TEXT,
                       text="\n".join([text, *(label for _, label in items)]))
            return True
        return self._send(kf, user, choices.numbered(text, reply.options), inbound_msgid, "reply", reply.files)

    def _cut(self, parts: List[str], files: List[ReplyFile], left: int):
        """只能再发 left 条：文字先发，最后一段文字后追加继续的提示（放不进去就重新切，不让它超过单条字节数）。
        返回 (现在发的文字段, 现在发的文件, (没发的文字, 没发的文件))。left 为 0 时整条都存起来，用户那边看不到提示。"""
        if left == 0:
            return [], [], ("".join(parts), files)
        hint = HINT.format(n=self._cfg.quota_per_window, key=CONTINUE)
        texts = split_text("".join(parts), self._cfg.text_max_bytes - len(hint.encode("utf-8")) - 2)
        k = min(len(texts), left)
        if k:
            now_parts = texts[:k - 1] + [texts[k - 1].rstrip() + "\n\n" + hint]
            return now_parts, files[:left - k], ("".join(texts[k:]), files[left - k:])
        return [hint], files[:left - 1], ("", files[left - 1:])

    def _flush_held(self, kf: str, user: str, msgid: str) -> bool:
        """用户发来消息、配额重置后，先补发上次被配额截下的内容。补到一半出错，没发的放回去。"""
        items = self._state.take_held(kf, user)
        for i, raw in enumerate(items):
            try:
                text, files = _load(raw)
                self._send(kf, user, text, msgid, f"held{i}", files)
            except EvaError:
                for rest in items[i:]:
                    self._state.hold(kf, user, rest, self._window_seconds)
                raise
        return bool(items)

    def _notify_failure(self, msg: dict, error: EvaError) -> None:
        resets_at = error.details.get("resets_at") if error.code is Code.BUDGET_EXCEEDED else None
        text = (QUOTA_EXHAUSTED.format(when=datetime.fromisoformat(resets_at).strftime("%m月%d日 %H:%M")) if resets_at
                else f"这条消息没有处理成功（{error.code.value}），请稍后重试或换个说法。")
        try:
            self._send(msg["open_kfid"], msg["external_userid"], text, msg["msgid"], "error")
        except EvaError as e:  # 通知本身失败不能再掩盖原错误，两个都上报
            self._report(e, {"where": "notify", "msgid": msg.get("msgid")})


def _dump(text: str, files: Sequence[ReplyFile]) -> str:
    return json.dumps({"text": text, "files": [{"filename": f.filename, "mime": f.mime, "inline": f.inline_if_short,
                                                "file_id": f.file_id, "data": base64.b64encode(f.data).decode("ascii")} for f in files]}, ensure_ascii=False)


def _load(raw: str) -> Tuple[str, List[ReplyFile]]:
    d = json.loads(raw)
    return d["text"], [ReplyFile(f["filename"], f["mime"], base64.b64decode(f["data"]), f["inline"], f.get("file_id")) for f in d["files"]]


def _clip(text: str, max_bytes: int) -> str:
    """菜单项文字超过上限时从尾部截掉，加省略号。开头的序号保留，用户点击后的回复靠序号对回选项。"""
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    while len((text + "…").encode("utf-8")) > max_bytes:
        text = text[:-1]
    return text + "…"


def _reply_text(reply: Reply) -> str:
    if reply.reason == TurnEnd.CANCELLED.value:
        return CANCELLED
    if reply.text.strip():
        return reply.text
    return "请查收文件。" if reply.files else f"本次没有产出文字回复（{reply.reason}）。"
