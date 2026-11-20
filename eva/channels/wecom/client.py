"""企微客服接口客户端：access_token、sync_msg、media/get、media/upload、send_msg（文本、文件）。

HTTP 由调用方注入（带 request(method, url, **kw) 的对象，requests 即可），所以测试里换成假服务端，不连真企微。
失败分三类，调用方靠错误码区分：网络、超时、5xx、限流抛 E7002（可重试）；企微返回业务错误抛 E7003（带 errcode）。
网络层只立刻重试一次，不做退避：事件队列的重投才是正式的重试机制。
"""
import re
import threading
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote

from eva.common import clock
from eva.channels.wecom.config import WecomConfig
from eva.contracts.errors import Code, EvaError

# 企微说"稍后重试"的错误码：系统繁忙、频率超限
_RETRYABLE_ERRCODES = {-1, 45009}
# access_token 失效，换一个再试一次
_TOKEN_ERRCODES = {40014, 42001, 40001}

_FILENAME_RFC5987 = re.compile(r"filename\*=utf-8'[^']*'([^;\r\n]+)", re.IGNORECASE)
_FILENAME_PLAIN = re.compile(r'filename="?([^";\r\n]+)"?', re.IGNORECASE)


def parse_filename(content_disposition: str) -> Optional[str]:
    """从 Content-Disposition 取用户上传时的文件名：先 RFC 5987 的 filename*，再普通 filename。
    取不到、或去掉扩展名后为空，返回 None（由调用方决定怎么命名，这里不编名字）。"""
    if not content_disposition:
        return None
    m = _FILENAME_RFC5987.search(content_disposition) or _FILENAME_PLAIN.search(content_disposition)
    if not m:
        return None
    name = unquote(m.group(1), encoding="utf-8").strip()
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return name if stem.strip() else None


class WecomClient:
    def __init__(self, cfg: WecomConfig, http, corp_id: str, secret: str, now: Callable[[], float] = clock.unix):
        if not corp_id or not secret:
            raise EvaError(Code.CONFIG_INVALID, "缺企微 corp_id 或客服 secret")
        self._cfg, self._http, self._corp_id, self._secret, self._now = cfg, http, corp_id, secret, now
        self._token, self._expires_at, self._lock = None, 0.0, threading.Lock()

    # ---- 底层 ----

    def _send(self, method: str, path: str, timeout: float, **kw):
        url = f"{self._cfg.api_base}{path}"
        for attempt in (1, 2):
            try:
                resp = self._http.request(method, url, timeout=timeout, **kw)
            except OSError as e:
                if attempt == 2:
                    raise EvaError(Code.CHANNEL_UNAVAILABLE, "企微接口网络失败", path=path, error=str(e)) from e
                continue
            if resp.status_code >= 500:
                if attempt == 2:
                    raise EvaError(Code.CHANNEL_UNAVAILABLE, "企微接口 5xx", path=path, status=resp.status_code)
                continue
            return resp

    @staticmethod
    def _check(path: str, data: dict) -> dict:
        errcode = data.get("errcode", 0)
        if not errcode:
            return data
        code = Code.CHANNEL_UNAVAILABLE if errcode in _RETRYABLE_ERRCODES else Code.CHANNEL_REJECTED
        raise EvaError(code, "企微接口返回错误", path=path, errcode=errcode, errmsg=data.get("errmsg"))

    def _json(self, path: str, resp) -> dict:
        try:
            data = resp.json()
        except ValueError as e:
            raise EvaError(Code.CHANNEL_UNAVAILABLE, "企微接口返回的不是 JSON", path=path) from e
        if not isinstance(data, dict):
            raise EvaError(Code.CHANNEL_UNAVAILABLE, "企微接口返回的不是对象", path=path)
        return data

    def _access_token(self, refresh: bool = False) -> str:
        with self._lock:
            if not refresh and self._token and self._now() < self._expires_at:
                return self._token
            path = "/gettoken"
            resp = self._send("GET", path, self._cfg.http_timeout_seconds,
                              params={"corpid": self._corp_id, "corpsecret": self._secret})
            data = self._check(path, self._json(path, resp))
            if not data.get("access_token") or not isinstance(data.get("expires_in"), (int, float)):
                raise EvaError(Code.CHANNEL_UNAVAILABLE, "gettoken 返回缺 access_token 或 expires_in")
            self._token = data["access_token"]
            self._expires_at = self._now() + data["expires_in"] - self._cfg.token_margin_seconds
            return self._token

    def _call(self, method: str, path: str, timeout: float, parse: Callable, **kw):
        """带 access_token 调接口；token 失效换一个重试一次。parse 把响应变成结果，业务错误在其中抛出。"""
        base = kw.pop("params", {})
        for refresh in (False, True):
            params = {**base, "access_token": self._access_token(refresh)}
            resp = self._send(method, path, timeout, params=params, **kw)
            try:
                return parse(resp)
            except EvaError as e:
                if e.code is Code.CHANNEL_REJECTED and e.details.get("errcode") in _TOKEN_ERRCODES and not refresh:
                    continue
                raise

    # ---- 接口 ----

    def sync_msg(self, kf_token: str, open_kfid: str, cursor: str) -> Tuple[List[dict], str, bool]:
        """拉一页消息，返回 (消息列表, 新游标, 还有没有下一页)。游标由调用方在消息处理完后再保存。kf_token 为空串时不带 token（对账用）。"""
        path = "/kf/sync_msg"

        def parse(resp):
            data = self._check(path, self._json(path, resp))
            msgs = data.get("msg_list", [])
            if not isinstance(msgs, list) or not isinstance(data.get("next_cursor"), str):
                raise EvaError(Code.CHANNEL_UNAVAILABLE, "sync_msg 返回缺 msg_list 或 next_cursor")
            return msgs, data["next_cursor"], bool(data.get("has_more"))

        body = {"cursor": cursor, "open_kfid": open_kfid, "limit": self._cfg.sync_limit}
        if kf_token:  # 对账时没有回调给的 token；不带也能拉，只是企微的频率限制更严
            body["token"] = kf_token
        return self._call("POST", path, self._cfg.http_timeout_seconds, parse, json=body)

    def get_media(self, media_id: str) -> Tuple[bytes, str, Optional[str]]:
        """下载临时素材，返回 (内容, Content-Type, 文件名或 None)。素材 3 天有效，要立刻取。
        成功时响应是二进制流，失败时是 JSON 错误体，靠 Content-Type 区分。"""
        path = "/media/get"

        def parse(resp):
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype:
                self._check(path, self._json(path, resp))
                raise EvaError(Code.CHANNEL_UNAVAILABLE, "media/get 返回了 JSON 却没有错误码", path=path)
            return resp.content, ctype, parse_filename(resp.headers.get("Content-Disposition", ""))

        return self._call("GET", path, self._cfg.media_timeout_seconds, parse, params={"media_id": media_id})

    def send_text(self, touser: str, open_kfid: str, content: str, msgid: str) -> str:
        """发一条文本。长度上限由调用方切分保证，这里超了直接报错而不是截断。"""
        if len(content.encode("utf-8")) > self._cfg.text_max_bytes:
            raise EvaError(Code.INVALID_VALUE, "文本超过单条上限", limit=self._cfg.text_max_bytes)
        path = "/kf/send_msg"

        def parse(resp):
            data = self._check(path, self._json(path, resp))
            return data.get("msgid", msgid)

        body: Dict = {"touser": touser, "open_kfid": open_kfid, "msgid": msgid, "msgtype": "text",
                      "text": {"content": content}}
        return self._call("POST", path, self._cfg.http_timeout_seconds, parse, json=body)

    def send_menu(self, touser: str, open_kfid: str, head: str, items: Sequence[Tuple[str, str]], msgid: str) -> str:
        """发一条菜单消息，items 是（菜单 ID, 显示文字）。点击后用户端回调一条 text 消息，text.menu_id 是被点的菜单 ID，内容是显示文字。"""
        path = "/kf/send_msg"

        def parse(resp):
            return self._check(path, self._json(path, resp)).get("msgid", msgid)

        body: Dict = {"touser": touser, "open_kfid": open_kfid, "msgid": msgid, "msgtype": "msgmenu",
                      "msgmenu": {"head_content": head, "list": [{"type": "click", "click": {"id": i, "content": t}} for i, t in items]}}
        return self._call("POST", path, self._cfg.http_timeout_seconds, parse, json=body)

    def upload_media(self, data: bytes, filename: str) -> str:
        """上传临时素材（type=file）拿 media_id，3 天内有效。大小上限由调用方保证，这里不判断。"""
        path = "/media/upload"

        def parse(resp):
            media_id = self._check(path, self._json(path, resp)).get("media_id")
            if not isinstance(media_id, str) or not media_id:
                raise EvaError(Code.CHANNEL_UNAVAILABLE, "media/upload 返回缺 media_id", path=path)
            return media_id

        return self._call("POST", path, self._cfg.media_timeout_seconds, parse, params={"type": "file"},
                          files={"media": (filename, data)})

    def send_file(self, touser: str, open_kfid: str, media_id: str, msgid: str) -> str:
        path = "/kf/send_msg"

        def parse(resp):
            return self._check(path, self._json(path, resp)).get("msgid", msgid)

        body: Dict = {"touser": touser, "open_kfid": open_kfid, "msgid": msgid, "msgtype": "file",
                      "file": {"media_id": media_id}}
        return self._call("POST", path, self._cfg.http_timeout_seconds, parse, json=body)
