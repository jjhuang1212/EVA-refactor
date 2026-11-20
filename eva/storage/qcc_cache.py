"""qcc_cache 表（公共层）：QCC 企业数据查询的缓存，所有租户共享，防止同一家公司的同一维度反复付费。

唯一写入口是 query_company 拿到 QCC 返回之后的 put，只收固定结构：记录列表（每条是对象）和截断情况，不收自由文本。
"""
import json
import re
from datetime import datetime
from typing import List, Optional

from eva.common import clock
from eva.contracts.errors import Code, EvaError
from eva.storage.repo import FK_VIOLATION, TenantRepo

_DIMENSION = re.compile(r"^[a-z][a-z_]{0,31}$")
_MAX_PERSON = 128


class QccCache(TenantRepo):
    def get(self, uscc: str, dimension: str, person: str) -> Optional[dict]:
        """返回 {"rows", "cut", "fetched_at"}；没有缓存返回 None。新鲜与否由调用方按配置判断。"""
        r = self._execute("SELECT payload, fetched_at FROM qcc_cache WHERE uscc = %s AND dimension = %s AND person = %s",
                          (uscc, dimension, person)).fetchone()
        if r is None:
            return None
        body = json.loads(r[0])
        return {"rows": body["rows"], "cut": body["cut"], "fetched_at": r[1]}

    def put(self, uscc: str, dimension: str, person: str, rows: List[dict], cut: Optional[int]) -> datetime:
        """存一次查询结果，已有的整行覆盖。返回写入的取数时间。"""
        if (not _DIMENSION.match(dimension or "") or not isinstance(person, str) or len(person) > _MAX_PERSON
                or not isinstance(rows, list) or not all(isinstance(x, dict) for x in rows)
                or not (cut is None or (isinstance(cut, int) and not isinstance(cut, bool) and cut >= 0))):
            raise EvaError(Code.INVALID_VALUE, "查询缓存只收维度名、人名和结构化的记录列表", dimension=repr(dimension))
        now, payload = clock.now(), json.dumps({"rows": rows, "cut": cut}, ensure_ascii=False)
        not_found = {FK_VIOLATION: EvaError(Code.COMPANY_NOT_FOUND, "公司不在主数据里", uscc=uscc)}
        cur = self._execute("UPDATE qcc_cache WITH (UPDLOCK, HOLDLOCK) SET payload = %s, fetched_at = %s "
                            "WHERE uscc = %s AND dimension = %s AND person = %s", (payload, now, uscc, dimension, person))
        if cur.rowcount == 0:
            self._execute("INSERT INTO qcc_cache (uscc, dimension, person, payload, fetched_at) "
                          "SELECT %s, %s, %s, %s, %s WHERE NOT EXISTS (SELECT 1 FROM qcc_cache WITH (UPDLOCK, HOLDLOCK) "
                          "WHERE uscc = %s AND dimension = %s AND person = %s)",
                          (uscc, dimension, person, payload, now, uscc, dimension, person), on_integrity=not_found)
        return now
