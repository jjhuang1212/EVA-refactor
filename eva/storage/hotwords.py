"""ASR 热词（公共层，只读）：全局公共，离线脚本维护。绑定租户只是为了统一入口的校验，查询不带租户条件，因为表里没有租户数据。"""
from typing import List

from eva.storage.repo import TenantRepo


class Hotwords(TenantRepo):
    def all(self) -> List[dict]:
        rows = self._execute("SELECT term, weight, source_kind, industry FROM asr_hotwords").fetchall()
        return [{"term": t, "weight": float(w), "source_kind": k, "industry": i} for t, w, k, i in rows]
