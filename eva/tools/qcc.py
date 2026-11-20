"""QCC（企查查）MCP 客户端：按名字找公司，按维度查公司数据。

传输是 Streamable HTTP：每个 server（company、executive…）一个端点、一个会话（服务端把 session 放在 cookie 里，
所以每个 server 一个 requests.Session），先 initialize 握手一次，再逐次 tools/call，应答是只含一条 `data: {...}` 的 SSE。
查无此公司是空结果，服务方故障才抛错误码；返回里缺名字或 USCC 形制不对的候选整次报错（USCC 是后面所有穿透查询的键）；
USCC 是空串的候选（境外机构的代表处等，本来就没有境内信用代码）不能当实体，直接略去。
"""
import json
import re
import threading
from dataclasses import dataclass
from typing import Dict, Tuple

import requests

from eva.contracts.errors import Code, EvaError
from eva.tools.config import QccConfig

_BY_QUERY = ("company", "get_company_by_query")
_UNIQUE, _MULTI, _NONE = "唯一精确匹配", "多候选", "未匹配"
USCC = re.compile(r"^[0-9A-Z]{18}$")
_ERROR_TEXT_CHARS = 200

_gates: dict = {}
_gates_lock = threading.Lock()


@dataclass(frozen=True)
class Hit:
    name: str
    uscc: str
    raw: dict  # QCC 对这家公司的原始记录，写公共层 data_json 用


@dataclass(frozen=True)
class Outcome:
    unique: bool  # 服务端判定为唯一精确匹配
    hits: Tuple[Hit, ...]


class _Server:
    def __init__(self, http):
        self.http, self.lock, self.ready = http, threading.Lock(), False


class Qcc:
    def __init__(self, cfg: QccConfig, api_key: str, http=None):
        """http 只给测试注入假的；真实使用时每个 server 各建一个 Session，cookie 不互串。"""
        self.cfg, self._key, self._injected = cfg, api_key, http
        self._servers: Dict[str, _Server] = {}
        self._servers_lock = threading.Lock()
        with _gates_lock:  # 全进程共用一个并发闸，所有租户共用一个服务方额度
            self._gate = _gates.setdefault(cfg.max_concurrency, threading.BoundedSemaphore(cfg.max_concurrency))

    def search(self, name: str) -> Outcome:
        return _outcome(self.call(*_BY_QUERY, {"searchKey": name}))

    def call(self, server: str, tool: str, arguments: dict) -> dict:
        """调一个工具，返回它的 JSON 结果。网络、鉴权、协议问题抛错误码；业务上的"没匹配到"由调用方从结果里判。"""
        if not self._gate.acquire(timeout=self.cfg.timeout_seconds):
            raise EvaError(Code.ENTITY_UNAVAILABLE, "企业数据源排队超时")
        try:
            state = self._state(server)
            self._handshake(server, state)
            return self._tool_payload(self._rpc(server, state, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                                "params": {"name": tool, "arguments": arguments}}))
        finally:
            self._gate.release()

    def _state(self, server: str) -> _Server:
        with self._servers_lock:
            if server not in self._servers:
                self._servers[server] = _Server(self._injected or requests.Session())
            return self._servers[server]

    def _handshake(self, server: str, state: _Server) -> None:
        with state.lock:
            if not state.ready:
                self._rpc(server, state, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                                     "clientInfo": {"name": "eva", "version": "1"}}})
                state.ready = True

    def _rpc(self, server: str, state: _Server, body: dict) -> dict:
        try:
            resp = state.http.post(self.cfg.url(server), json=body, timeout=self.cfg.timeout_seconds, headers={
                "Authorization": f"Bearer {self._key}", "Accept": "application/json, text/event-stream"})
        except requests.RequestException as e:
            state.ready = False
            raise EvaError(Code.ENTITY_UNAVAILABLE, "企业数据源请求失败", reason=type(e).__name__) from e
        status = resp.status_code
        if status != 200:
            state.ready = False  # session 可能过期，下次重新握手
            if status in (401, 403) or (400 <= status < 500 and status != 429):
                raise EvaError(Code.ENTITY_REJECTED, "企业数据源拒绝了请求", status=status, reason=str(resp.text or "")[:_ERROR_TEXT_CHARS])
            raise EvaError(Code.ENTITY_UNAVAILABLE, "企业数据源繁忙或不可用", status=status)
        # 应答没声明字符集，requests 会按 ISO-8859-1 解成乱码，乱码里的 \x85 还会被 splitlines 当换行切断数据行
        for line in resp.content.decode("utf-8", errors="replace").split("\n"):
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:])
                except ValueError:
                    break
                if isinstance(msg, dict):
                    return msg
        raise EvaError(Code.ENTITY_UNAVAILABLE, "企业数据源返回的不是预期的 SSE", status=status)

    @staticmethod
    def _tool_payload(msg: dict) -> dict:
        err = msg.get("error")
        if err is not None:
            # 没开通的服务（如历史信息）是账号权限问题，重试没用
            denied = isinstance(err, dict) and "未授权" in str(err.get("message", ""))
            raise EvaError(Code.ENTITY_REJECTED if denied else Code.ENTITY_UNAVAILABLE, "企业数据源返回错误", reason=str(err)[:_ERROR_TEXT_CHARS])
        result = msg.get("result")
        for item in (result.get("content") or ()) if isinstance(result, dict) else ():
            text = item.get("text") if isinstance(item, dict) else None
            if isinstance(text, str) and text.strip():
                try:
                    parsed = json.loads(text)
                except ValueError:
                    raise EvaError(Code.ENTITY_UNAVAILABLE, "企业数据源的结果不是 JSON") from None
                if isinstance(parsed, dict):
                    return parsed
        raise EvaError(Code.ENTITY_UNAVAILABLE, "企业数据源返回里没有结果内容")


def _hit(record) -> Hit:
    name = record.get("企业名称") if isinstance(record, dict) else None
    uscc = record.get("统一社会信用代码") if isinstance(record, dict) else None
    if not isinstance(name, str) or not name.strip() or not isinstance(uscc, str) or not USCC.match(uscc.strip()):
        raise EvaError(Code.ENTITY_UNAVAILABLE, "企业数据源的候选缺名称或信用代码不合形制", got=str(record)[:_ERROR_TEXT_CHARS])
    return Hit(name.strip(), uscc.strip(), record)


def _no_code(record) -> bool:
    return isinstance(record, dict) and isinstance(record.get("统一社会信用代码"), str) and not record["统一社会信用代码"].strip()


def _outcome(payload: dict) -> Outcome:
    verdict, info = payload.get("匹配结果"), payload.get("企业信息")
    if verdict == _NONE:
        return Outcome(False, ())
    if verdict == _UNIQUE and isinstance(info, dict):
        return Outcome(False, ()) if _no_code(info) else Outcome(True, (_hit(info),))
    if verdict == _MULTI and isinstance(info, list):
        return Outcome(False, tuple(_hit(r) for r in info if not _no_code(r)))
    raise EvaError(Code.ENTITY_UNAVAILABLE, "企业数据源的匹配结果不认识", verdict=str(verdict)[:50])
