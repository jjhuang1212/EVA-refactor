"""企微回调：GET 验签握手，POST 收事件。

只做验签、解密、把 token 入队，立刻返回，不拉消息、不落库、不调模型（企微 5 秒不回就重试，硬要求 1 秒内回 200）。
返回 (HTTP 状态, 响应体)，框架层（Flask 等）在 eva/app.py 里套，这里不依赖框架，方便不起服务就测。
"""
from typing import Callable, Mapping, Tuple

from eva.channels.wecom import crypto
from eva.contracts.errors import Code, EvaError


class Callback:
    def __init__(self, token: str, encoding_aes_key: str, push: Callable[[str, str], None]):
        if not token or not encoding_aes_key:
            raise EvaError(Code.CONFIG_INVALID, "缺企微回调 Token 或 EncodingAESKey")
        self._token, self._key, self._push = token, encoding_aes_key, push

    def verify(self, query: Mapping[str, str]) -> Tuple[int, str]:
        """URL 验证：验签后原样返回解密出的 echostr。"""
        return self._reply(lambda: self._decrypt(query, query.get("echostr", "")))

    def receive(self, query: Mapping[str, str], body: bytes) -> Tuple[int, str]:
        """事件推送。目前只处理 kf_msg_or_event；别的事件类型不入队，直接回 200，因为企微只要 200 就不再重试。"""
        def run():
            outer = crypto.parse_xml(body)
            event = crypto.parse_xml(self._decrypt(query, outer.get("Encrypt", "")).encode("utf-8"))
            if event.get("MsgType") == "event" and event.get("Event") == "kf_msg_or_event":
                token, kfid = event.get("Token"), event.get("OpenKfId")
                if not token or not kfid:
                    raise EvaError(Code.CALLBACK_REJECTED, "kf_msg_or_event 缺 Token 或 OpenKfId", status=400)
                self._push(token, kfid)
            return "success"

        return self._reply(run)

    def _decrypt(self, query: Mapping[str, str], encrypted: str) -> str:
        crypto.check_signature(self._token, query.get("timestamp", ""), query.get("nonce", ""), encrypted,
                               query.get("msg_signature", ""))
        return crypto.decrypt(self._key, encrypted)

    @staticmethod
    def _reply(run: Callable[[], str]) -> Tuple[int, str]:
        """验签、解密失败是对方的错，回 403/400 让企微知道；入队失败（Redis 挂了）是我们的错，回 500 让企微重推。"""
        try:
            return 200, run()
        except EvaError as e:
            if e.code is Code.CALLBACK_REJECTED:
                return e.details["status"], e.message
            return 500, e.code.value
