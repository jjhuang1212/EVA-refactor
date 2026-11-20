"""企微回调的验签、AES 解密、XML 解析。算法由企微固定，不是设计选择。

失败统一抛 E7001，details.status 是该回给企微的 HTTP 状态：验签失败 403，解密或解析失败 400。
"""
import base64
import hashlib
import hmac
import struct
import xml.etree.ElementTree as ET
from typing import Dict

from Crypto.Cipher import AES

from eva.contracts.errors import Code, EvaError


def check_signature(token: str, timestamp: str, nonce: str, encrypted: str, signature: str) -> None:
    """sha1(sort(token, timestamp, nonce, 密文))。不符抛 E7001（403）。"""
    digest = hashlib.sha1("".join(sorted([token, timestamp, nonce, encrypted])).encode("utf-8")).hexdigest()
    if not hmac.compare_digest(digest, signature or ""):
        raise EvaError(Code.CALLBACK_REJECTED, "回调验签失败", status=403)


def decrypt(encoding_aes_key: str, encrypted: str) -> str:
    """AES-256-CBC 解密，GET 校验的 echostr 和 POST 的 Encrypt 通用。失败抛 E7001（400）。"""
    try:
        key = base64.b64decode(encoding_aes_key + "=")
        plain = AES.new(key, AES.MODE_CBC, key[:16]).decrypt(base64.b64decode(encrypted))
        plain = plain[:-plain[-1]]
        size = struct.unpack(">I", plain[16:20])[0]
        if 20 + size > len(plain):
            raise ValueError("消息长度超出明文")
        return plain[20:20 + size].decode("utf-8")
    except Exception as e:  # 密码库的各种异常对调用方都是同一件事：解密失败
        raise EvaError(Code.CALLBACK_REJECTED, "回调解密失败", status=400, reason=type(e).__name__) from e


def encrypt(encoding_aes_key: str, plain: str, receive_id: str) -> str:
    """企微加密的逆过程。生产里不用，测试用它构造合法回调。"""
    key = base64.b64decode(encoding_aes_key + "=")
    body = plain.encode("utf-8")
    raw = b"0123456789abcdef" + struct.pack(">I", len(body)) + body + receive_id.encode("utf-8")
    pad = 32 - len(raw) % 32
    raw += bytes([pad]) * pad
    return base64.b64encode(AES.new(key, AES.MODE_CBC, key[:16]).encrypt(raw)).decode()


def parse_xml(data: bytes) -> Dict[str, str]:
    """单层 XML 转字典。"""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise EvaError(Code.CALLBACK_REJECTED, "回调不是合法 XML", status=400) from e
    return {child.tag: (child.text or "") for child in root}
