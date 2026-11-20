"""tenants 表。租户由 Identities.resolve 在第一次见到某个渠道身份时建，不单独对外建。"""
from typing import Optional

from eva.common import clock, ids
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import TenantRealm, check
from eva.storage.repo import execute, require_tenant


def get(conn, tenant_id: str) -> Optional[dict]:
    require_tenant(tenant_id)
    r = execute(conn, "SELECT tenant_id, created_at FROM tenants WHERE tenant_id = %s", (tenant_id,)).fetchone()
    return dict(zip(("tenant_id", "created_at"), r)) if r else None


def realm_of(tenant_id: str) -> Optional[TenantRealm]:
    """tenant_id 的前缀对应的用途；前缀不在集合里（测试里的固定编号等）返回 None。"""
    head = require_tenant(tenant_id).split("_", 1)[0]
    return next((r for r in TenantRealm if r.value == head), None)


def require_realm(tenant_id: str, realm: TenantRealm) -> str:
    """写入只允许落在指定用途的租户上：别的用途的租户，哪怕存在也不碰。"""
    if realm_of(tenant_id) is not realm:
        raise EvaError(Code.INVALID_VALUE, f"这项写入只能落在 {realm.value}_ 开头的租户上", tenant_id=tenant_id)
    return tenant_id


def create(conn, realm: str) -> dict:
    """新建一个租户，编号 <realm>_<小写 ULID>。只由 Identities.resolve 调用：租户从身份来，不凭空建。"""
    tenant_id = f"{check(TenantRealm, realm).value}_{ids.new_id().lower()}"
    execute(conn, "INSERT INTO tenants (tenant_id, created_at) VALUES (%s, %s)", (tenant_id, clock.now()))
    return get(conn, tenant_id)

