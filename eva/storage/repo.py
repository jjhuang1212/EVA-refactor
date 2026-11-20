"""仓储公共部分：租户校验与错误翻译。仓储不提交事务，边界由调用方控制。"""
from typing import Dict, Optional

import pymssql

from eva.contracts.errors import Code, EvaError
from eva.storage import db

UNIQUE_VIOLATION = (2627, 2601)
FK_VIOLATION = 547


def require_tenant(tenant_id) -> str:
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise EvaError(Code.TENANT_REQUIRED, "tenant_id 为空")
    return tenant_id


def execute(conn, sql: str, params=(), on_integrity: Optional[Dict[int, EvaError]] = None):
    """返回已执行的游标。完整性错误按编号映射成调用方指定的错误码，其余一律翻译。"""
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
    except pymssql.IntegrityError as e:
        number = e.args[0] if e.args else None
        mapped = (on_integrity or {}).get(number)
        raise (mapped or db.translate(e)) from e
    except pymssql.Error as e:
        raise db.translate(e) from e
    return cur


class TenantRepo:
    """绑定一个租户；所有 SQL 都由子类带上 tenant_id 条件。"""

    def __init__(self, conn, tenant_id):
        self.conn = conn
        self.tenant_id = require_tenant(tenant_id)

    def _execute(self, sql, params=(), on_integrity=None):
        return execute(self.conn, sql, params, on_integrity)
