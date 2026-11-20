"""会话运行：同一会话同一时刻只有一个写入者，日志追加带期望序号。

进程内用一个占用表保证单写入者（MVP 单进程）；多 worker 时换成租约，接口不变。
序号对不上不在这里自动重读续写：写入者的视图已经过期，必须退出，重新进入时重读日志。
"""
import threading
from contextlib import contextmanager
from typing import Callable, Iterator, List, Optional

from eva.common import logs
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import EventType
from eva.storage import db
from eva.storage.repo import require_tenant
from eva.storage.sessions import Sessions
from eva.session.trace import Trace
from eva.storage.transcript import Transcript

_busy = set()
_guard = threading.Lock()


class SessionRunner:
    def __init__(self, tenant_id: str, session_id: str, open_tx: Callable = db.transaction):
        self.tenant_id, self.session_id = require_tenant(tenant_id), session_id
        self._open_tx = open_tx

    @contextmanager
    def writing(self) -> Iterator["Writer"]:
        """取得该会话的写入权。已有在途运行抛 E3003；会话不存在抛 E3002。"""
        key = (self.tenant_id, self.session_id)
        with _guard:
            if key in _busy:
                raise EvaError(Code.SESSION_BUSY, "会话已有在途运行", session_id=self.session_id)
            _busy.add(key)
        try:
            with logs.bind(tenant=self.tenant_id, session=self.session_id):
                yield Writer(self.tenant_id, self.session_id, self._open_tx)
        finally:
            with _guard:
                _busy.discard(key)


class Writer:
    def __init__(self, tenant_id: str, session_id: str, open_tx: Callable):
        self.tenant_id, self.session_id, self._open_tx = tenant_id, session_id, open_tx
        self._trace = Trace()
        with open_tx() as conn:
            Sessions(conn, tenant_id).require(session_id)
            self._next = Transcript(conn, tenant_id).next_seq(session_id)

    def events(self, from_seq: int = 0) -> List[dict]:
        with self._open_tx() as conn:
            return Transcript(conn, self.tenant_id).read(self.session_id, from_seq)

    def live(self) -> List[dict]:
        """模型视图用得到的事件（见 Transcript.read_live）。循环每一步都读，不读被摘要代替的部分。"""
        with self._open_tx() as conn:
            return Transcript(conn, self.tenant_id).read_live(self.session_id)

    def append(self, event_type: str, payload: dict) -> int:
        """追加一条事件并提交，返回序号。别的写入者抢先写了就抛 E3001，此时本写入者的视图已过期。"""
        with self._open_tx() as conn:
            seq = Transcript(conn, self.tenant_id).append(self.session_id, self._next, event_type, payload)
        self._next = seq + 1
        self._trace.record(EventType(event_type), seq, payload)
        return seq

    def append_user_message(self, msg_id: str, text: str) -> Optional[int]:
        """渠道消息 ID 已记过就返回 None，不重复写；否则追加并返回序号。
        查重和追加分属两个事务也安全：同一会话只有一个写入者，跨进程的竞争由期望序号兜底。"""
        with self._open_tx() as conn:
            if Transcript(conn, self.tenant_id).find_user_message(self.session_id, msg_id) is not None:
                return None
        return self.append(EventType.USER_MESSAGE.value, {"msg_id": msg_id, "text": text})

    def append_input(self, msg_id: str, text: str, parking_tools=()) -> Optional[int]:
        """用户的一条输入。有停放中的调用在等回答，就写成它的 tool_result；否则写成用户消息。
        已记过的 msg_id 返回 None（含已经作为回答写入的）。"""
        events = self.live()
        answered = {e["payload"].get("call_id") for e in events if e["event_type"] == EventType.TOOL_RESULT.value}
        waiting = [(c["id"], c["name"]) for e in events if e["event_type"] == EventType.ASSISTANT_MESSAGE.value
                   for c in e["payload"].get("tool_calls") or []
                   if c["name"] in parking_tools and c["id"] not in answered]
        if len(waiting) > 1:
            raise EvaError(Code.STORAGE_ERROR, "同时有多个停放中的调用，日志已损坏", call_ids=[w[0] for w in waiting])
        if not waiting:
            return self.append_user_message(msg_id, text)
        with self._open_tx() as conn:
            if Transcript(conn, self.tenant_id).find_user_message(self.session_id, msg_id) is not None:
                return None
        return self.append(EventType.TOOL_RESULT.value,
                           {"call_id": waiting[0][0], "name": waiting[0][1], "content": text, "is_error": False,
                            "msg_id": msg_id})

    def recover_tool_calls(self, exempt_tools=()) -> int:
        """给"有调用没结果"的工具调用补一条合成结果，返回补了几条。
        没写过 tool_start 的可以放心重试；写过的结果未知，有副作用的操作要先核实。
        exempt_tools 里的工具不补（例如停放等待用户回答的 ask_user，它的结果就是用户的回复）。
        """
        calls, started, done = {}, set(), set()
        for e in self.live():
            kind, p = e["event_type"], e["payload"]
            if kind == EventType.ASSISTANT_MESSAGE.value:  # 写完模型输出、还没来得及写 tool_call 就崩溃的也要补
                for c in p.get("tool_calls") or []:
                    calls[c["id"]] = c["name"]
                continue
            if kind not in (EventType.TOOL_CALL.value, EventType.TOOL_START.value, EventType.TOOL_RESULT.value):
                continue
            call_id = p.get("call_id") if isinstance(p, dict) else None
            if not isinstance(call_id, str) or not call_id:
                raise EvaError(Code.STORAGE_ERROR, "工具事件缺 call_id，日志已损坏", seq=e["seq"])
            if kind == EventType.TOOL_CALL.value:
                calls[call_id] = p.get("name")
            elif kind == EventType.TOOL_START.value:
                started.add(call_id)
            else:
                done.add(call_id)
        count = 0
        for call_id, name in calls.items():
            if call_id in done or name in exempt_tools:
                continue
            unknown = call_id in started
            self.append(EventType.TOOL_RESULT.value, {
                "call_id": call_id, "synthetic": True,
                "status": "unknown_outcome" if unknown else "not_started", "retry_safe": not unknown,
                "message": "上次运行中断，结果未知，有副作用的操作先核实外部状态" if unknown
                else "上次运行中断，该调用未开始执行，可以重试",
            })
            count += 1
        return count

