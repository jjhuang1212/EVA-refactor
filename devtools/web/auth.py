"""共享密码登录。没登录、过期、密码错和被锁定都抛 LOGIN_REQUIRED，reason 区分。"""
import hmac
import os
from typing import Callable, Dict, List

from eva.common import clock
from eva.contracts.errors import Code, EvaError
from devtools.web.config import WebConfig

PASSWORD_ENV = "EVA_DEVTOOLS_PASSWORD"


def password_from_env() -> str:
    value = os.environ.get(PASSWORD_ENV)
    if not value:
        raise EvaError(Code.CONFIG_INVALID, "缺少EVA测评系统的登录密码环境变量", missing=[PASSWORD_ENV])
    return value


class Auth:
    def __init__(self, password: str, cfg: WebConfig, now: Callable[[], float] = clock.unix):
        self._password, self._cfg, self._now = password.encode("utf-8"), cfg, now
        self._failures: Dict[str, List[float]] = {}  # 来源地址 → 窗口内的失败时刻

    def login(self, source: str, given: str) -> float:
        """密码对则返回登录时刻，调用方记进会话 cookie。"""
        now = self._now()
        recent = [t for t in self._failures.get(source, []) if now - t < self._cfg.login_lock_seconds]
        if len(recent) >= self._cfg.login_max_failures:
            raise EvaError(Code.LOGIN_REQUIRED, "失败次数过多，已暂时锁定", reason="locked",
                           retry_after_seconds=int(self._cfg.login_lock_seconds - (now - recent[0])) + 1)
        if not isinstance(given, str) or not hmac.compare_digest(given.encode("utf-8"), self._password):
            self._failures[source] = recent + [now]
            raise EvaError(Code.LOGIN_REQUIRED, "密码不对", reason="bad_password")
        self._failures.pop(source, None)
        return now

    def check(self, logged_at) -> None:
        """logged_at 是登录时记在会话 cookie 里的时刻；没有或过期都要重新登录。"""
        if not isinstance(logged_at, (int, float)):
            raise EvaError(Code.LOGIN_REQUIRED, "请先登录", reason="not_logged_in")
        if self._now() - logged_at > self._cfg.session_ttl_seconds:
            raise EvaError(Code.LOGIN_REQUIRED, "登录已过期", reason="expired")
