"""tenant_identities 表：渠道里的"这个人是谁"到租户的对应。

Identities 构造时固定 realm，读写都只在这个 realm 内：模拟器、评测查不到真实用户的身份，失败表现为"查不到"。
"""
from typing import List, Optional

from eva.common import clock
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import Channel, TenantRealm, check
from eva.storage import tenants
from eva.storage.repo import execute

MAX_SCOPE, MAX_EXTERNAL_ID = 64, 128
LIST_LIMIT = 500


def external_prefix(realm: TenantRealm) -> str:
    """非真实用户的渠道用户 ID 必须带的前缀；真实用户用渠道给的原值，没有前缀。"""
    return "" if realm is TenantRealm.USR else f"{realm.value}_"


class Identities:
    def __init__(self, conn, realm: str):
        self.conn, self.realm = conn, check(TenantRealm, realm)

    def _check(self, channel: str, scope: str, external_id: str) -> str:
        channel = check(Channel, channel).value
        if not isinstance(scope, str) or len(scope) > MAX_SCOPE:
            raise EvaError(Code.INVALID_VALUE, f"scope 必须是不超过 {MAX_SCOPE} 字符的字符串", field="scope")
        prefix = external_prefix(self.realm)
        if (not isinstance(external_id, str) or len(external_id) > MAX_EXTERNAL_ID or not external_id.strip()
                or not external_id.startswith(prefix) or len(external_id) == len(prefix)
                or any(c.isspace() or ord(c) < 32 for c in external_id)):
            raise EvaError(Code.INVALID_VALUE, f"渠道用户 ID 必须是不超过 {MAX_EXTERNAL_ID} 字符、不含空白、"
                           f"以 {prefix!r} 开头且前缀后非空的字符串", field="external_id", realm=self.realm.value)
        return channel

    def _find(self, channel: str, scope: str, external_id: str):
        return execute(self.conn, "SELECT tenant_id FROM tenant_identities WHERE realm = %s AND channel = %s "
                                  "AND scope = %s AND external_id = %s",
                       (self.realm.value, channel, scope, external_id)).fetchone()

    def find(self, channel: str, scope: str, external_id: str) -> Optional[str]:
        """只读：身份对应的租户编号，没见过这个身份返回 None，不建租户。"""
        r = self._find(self._check(channel, scope, external_id), scope, external_id)
        return r[0] if r else None

    def resolve(self, channel: str, scope: str, external_id: str) -> str:
        """身份对应的租户编号，第一次见到就建租户和身份。并发首建时只有一方的行留下，另一方读到它的。"""
        channel = self._check(channel, scope, external_id)
        r = self._find(channel, scope, external_id)
        if r:
            return r[0]
        tenant_id = tenants.create(self.conn, self.realm.value)["tenant_id"]
        inserted = execute(
            self.conn,
            "INSERT INTO tenant_identities (realm, channel, scope, external_id, tenant_id, created_at) "
            "SELECT %s, %s, %s, %s, %s, %s WHERE NOT EXISTS (SELECT 1 FROM tenant_identities WITH (UPDLOCK, HOLDLOCK) "
            "WHERE realm = %s AND channel = %s AND scope = %s AND external_id = %s)",
            (self.realm.value, channel, scope, external_id, tenant_id, clock.now(),
             self.realm.value, channel, scope, external_id)).rowcount
        if inserted:
            return tenant_id
        execute(self.conn, "DELETE FROM tenants WHERE tenant_id = %s", (tenant_id,))
        return self._find(channel, scope, external_id)[0]

    def list(self, channel: str) -> List[dict]:
        """本 realm、本渠道的全部身份，最近建的在前，最多 LIST_LIMIT 条。"""
        channel = check(Channel, channel).value
        cur = execute(self.conn, "SELECT TOP (%s) scope, external_id, tenant_id, created_at FROM tenant_identities "
                                 "WHERE realm = %s AND channel = %s ORDER BY created_at DESC, external_id",
                      (LIST_LIMIT, self.realm.value, channel))
        return [dict(zip(("scope", "external_id", "tenant_id", "created_at"), r)) for r in cur.fetchall()]
