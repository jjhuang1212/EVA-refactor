"""测评系统（devtools/）的只读入口：租户过滤规则的唯一例外，只能被 devtools/ 引用（tests 里有守卫）。

只给读方法（列出所有用途的租户、各评测租户的人工标注和认可版本），没有任何写。读某个租户的会话和日志仍走各自带租户条件的仓储，由调用方显式给出租户；
真实用户、模拟器、终端的租户都能这样只读查看，写入（标注、认可版本、评测运行）只落在 eval_ 租户上。
"""
from typing import List, Optional, Tuple

from eva.contracts.errors import Code, EvaError
from eva.storage.eval_marks import (ANNOTATION_COLS, LATEST_ANNOTATION, LATEST_VERSION, VERSION_COLS,
                                    annotation_row, version_row)
from eva.storage.repo import execute


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("[", "\\[")


class Developer:
    def __init__(self, conn):
        self.conn = conn

    def annotations(self, case_name: Optional[str] = None, inputs_sha: Optional[str] = None) -> List[dict]:
        """所有评测租户里每次运行当前生效的人工标注，可按案例或输入版本筛，旧的在前。"""
        sql = LATEST_ANNOTATION.format(cols=", ".join("a." + c for c in ANNOTATION_COLS))
        params = []
        for column, value in (("case_name", case_name), ("inputs_sha", inputs_sha)):
            if value is not None:
                sql, params = sql + f" AND a.{column} = %s", params + [value]
        return [annotation_row(r) for r in execute(self.conn, sql + " ORDER BY a.created_at", tuple(params)).fetchall()]

    def versions(self) -> List[dict]:
        """所有评测租户里认可过的输入版本（每个版本取当前生效的名字），旧的在前。"""
        sql = LATEST_VERSION.format(cols=", ".join("v." + c for c in VERSION_COLS)) + " ORDER BY v.created_at"
        return [version_row(r) for r in execute(self.conn, sql).fetchall()]

    def tenants(self, prefix: Optional[str] = None, limit: int = 500, search: Optional[str] = None, offset: int = 0) -> Tuple[List[dict], bool]:
        """所有租户（可按 ID 前缀筛，可按 search 模糊搜租户 ID 和任一渠道身份的渠道、范围、外部 ID，不分大小写），新的在前，
        带最早那个渠道身份（没有身份的旧租户为空）。返回 (本页行, 是否还有下一页)。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise EvaError(Code.INVALID_VALUE, "limit 必须是正整数", limit=limit)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise EvaError(Code.INVALID_VALUE, "offset 必须是非负整数", offset=offset)
        conds, params = [], []
        if prefix:
            conds.append("tenant_id LIKE %s ESCAPE '\\'")
            params.append(_like_escape(prefix) + "%")
        if search:
            like = "%" + _like_escape(search.lower()) + "%"
            ident = " OR ".join(f"LOWER(i.{c}) LIKE %s ESCAPE '\\'" for c in ("channel", "scope", "external_id"))
            conds.append("(LOWER(tenant_id) LIKE %s ESCAPE '\\' OR EXISTS (SELECT 1 FROM tenant_identities i "
                         f"WHERE i.tenant_id = tenants.tenant_id AND ({ident})))")
            params += [like] * 4
        where = ("WHERE " + " AND ".join(conds) + " ") if conds else ""
        first = "(SELECT TOP 1 {col} FROM tenant_identities i WHERE i.tenant_id = tenants.tenant_id ORDER BY i.created_at)"
        cols = ", ".join(first.format(col=c) for c in ("channel", "scope", "external_id"))
        cur = execute(self.conn, f"SELECT tenant_id, {cols}, created_at FROM tenants {where}"
                                 "ORDER BY created_at DESC, tenant_id OFFSET %s ROWS FETCH NEXT %s ROWS ONLY",
                      (*params, offset, limit + 1))
        rows = [dict(zip(("tenant_id", "channel", "scope", "external_id", "created_at"), r)) for r in cur.fetchall()]
        return rows[:limit], len(rows) > limit
