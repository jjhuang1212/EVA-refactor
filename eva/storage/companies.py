"""公司主数据与别名查询（公共层，只读）及租户私有的别名确认（tenant_aliases）。"""
import json
import re
import unicodedata
from typing import List, Optional, Set, Tuple

from eva.common import clock
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import AliasKind, AliasSource
from eva.storage.repo import FK_VIOLATION, TenantRepo

_USCC = re.compile(r"^[0-9A-Z]{18}$")
_MAX_NAME = 512  # company_master.full_name 的列宽
_SELECT = "SELECT m.uscc, m.full_name, m.short_name, {matched_by}, m.sec_code, m.data_json FROM company_master m "
_NAMES = ("uscc", "full_name", "short_name", "matched_by", "sec_code")


def normalize_name(text: str) -> str:
    """公司名的规范写法：全半角统一、去掉所有空白，括号统一成工商登记里用的全角。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).replace("(", "（").replace(")", "）")


class Companies(TenantRepo):
    def resolve(self, name: str) -> List[dict]:
        """把用户说的名字对齐到公司。本租户确认过的称呼优先，命中就只返回它；否则查公共的别名和主表名称。
        没有候选返回空列表，多个候选都返回，由调用方让用户选。
        """
        if not isinstance(name, str) or not name.strip():
            raise EvaError(Code.INVALID_VALUE, "名字为空")
        forms = tuple(dict.fromkeys((name.strip(), normalize_name(name))))  # 原写法和规范写法都比，库里的老数据括号可能是半角
        marks = ", ".join(["%s"] * len(forms))
        confirmed = self._execute(
            _SELECT.format(matched_by="'tenant_alias'")
            + f"JOIN tenant_aliases t ON t.uscc = m.uscc WHERE t.tenant_id = %s AND t.alias IN ({marks}) ORDER BY m.uscc",
            (self.tenant_id, *forms),
        ).fetchall()
        rows = confirmed or self._execute(
            _SELECT.format(
                matched_by=f"CASE WHEN m.full_name IN ({marks}) THEN 'full_name' WHEN m.short_name IN ({marks}) THEN 'short_name' "
                           "ELSE 'alias' END"
            )
            + f"WHERE m.full_name IN ({marks}) OR m.short_name IN ({marks}) "
              f"OR EXISTS (SELECT 1 FROM company_aliases a WHERE a.uscc = m.uscc AND a.alias IN ({marks})) ORDER BY m.uscc",
            forms * 5,
        ).fetchall()
        out = []
        for r in rows:
            legal = json.loads(r[5]).get("法定代表人名称") if r[5] else None  # 查过企查查才有；没存的就没有这个键
            out.append({**dict(zip(_NAMES, r[:5])), **({"legal_representative": legal} if legal else {})})
        return out

    def get(self, uscc: str) -> Optional[dict]:
        r = self._execute(
            "SELECT uscc, full_name, short_name, sec_code, exchange, listed_at, delisted_at "
            "FROM company_master WHERE uscc = %s",
            (uscc,),
        ).fetchone()
        names = ("uscc", "full_name", "short_name", "sec_code", "exchange", "listed_at", "delisted_at")
        return dict(zip(names, r)) if r else None

    def record_qcc(self, uscc: str, full_name: str, raw: dict) -> None:
        """公共层的唯一写入口：QCC 查到的一家公司。只收固定字段，不收自由文本，也不带租户。
        只补缺：主表没有就插入；有但 data_json 还空（旧库迁移来的）就回填；已有的数据不覆盖。"""
        if not _USCC.match(uscc or "") or not isinstance(full_name, str) or not full_name.strip() or len(full_name) > _MAX_NAME:
            raise EvaError(Code.INVALID_VALUE, "公司主数据的信用代码或名称不合规", uscc=repr(uscc))
        if not isinstance(raw, dict):
            raise EvaError(Code.INVALID_VALUE, "data_json 必须来自结构化记录")
        data, now = json.dumps(raw, ensure_ascii=False), clock.now()
        cur = self._execute("UPDATE company_master WITH (UPDLOCK, HOLDLOCK) SET data_json = %s, fetched_at = %s, updated_at = %s "
                            "WHERE uscc = %s AND data_json IS NULL", (data, now, now, uscc))
        if cur.rowcount == 0:
            self._execute("INSERT INTO company_master (uscc, full_name, data_json, fetched_at, updated_at) "
                          "SELECT %s, %s, %s, %s, %s WHERE NOT EXISTS "
                          "(SELECT 1 FROM company_master WITH (UPDLOCK, HOLDLOCK) WHERE uscc = %s)",
                          (uscc, full_name.strip(), data, now, now, uscc))

    def alias_conflicts(self, alias: str, uscc: str) -> List[dict]:
        """这个叫法如果指向 uscc，会不会和已有的撞：返回它已经指向的其他公司（主表全称、简称，或别的来源的公共别名）。
        离线清单自己写的 GROUP 别名不算，那一类由清单整体对账。"""
        forms = tuple(dict.fromkeys((alias.strip(), normalize_name(alias))))
        marks = ", ".join(["%s"] * len(forms))
        rows = self._execute(
            "SELECT m.uscc, m.full_name, "
            f"CASE WHEN m.full_name IN ({marks}) THEN 'full_name' WHEN m.short_name IN ({marks}) THEN 'short_name' ELSE 'alias' END "
            f"FROM company_master m WHERE m.uscc <> %s AND (m.full_name IN ({marks}) OR m.short_name IN ({marks}) OR EXISTS ("
            f"SELECT 1 FROM company_aliases a WHERE a.uscc = m.uscc AND a.alias IN ({marks}) "
            "AND NOT (a.alias_kind = %s AND a.source = %s))) ORDER BY m.uscc",
            forms * 2 + (uscc,) + forms * 3 + (AliasKind.GROUP.value, AliasSource.OFFLINE.value)).fetchall()
        return [dict(zip(("uscc", "full_name", "matched_by"), r)) for r in rows]

    def add_public_alias(self, uscc: str, alias: str, kind: AliasKind, source: AliasSource) -> bool:
        """公共别名的写入口（只给离线整理的清单用，模型和租户都没有）。别名按规范写法存；已有同样的一行返回 False，不覆盖。"""
        alias = normalize_name(alias) if isinstance(alias, str) else ""
        if not alias or len(alias) > _MAX_NAME or not _USCC.match(uscc or ""):
            raise EvaError(Code.INVALID_VALUE, "公共别名的信用代码或别名不合规", uscc=repr(uscc), alias=repr(alias))
        not_found = {FK_VIOLATION: EvaError(Code.COMPANY_NOT_FOUND, "公司不在主数据里", uscc=uscc)}
        cur = self._execute(
            "INSERT INTO company_aliases (uscc, alias, alias_kind, source, created_at) "
            "SELECT %s, %s, %s, %s, %s WHERE NOT EXISTS (SELECT 1 FROM company_aliases WITH (UPDLOCK, HOLDLOCK) "
            "WHERE uscc = %s AND alias = %s AND alias_kind = %s)",
            (uscc, alias, kind.value, source.value, clock.now(), uscc, alias, kind.value), on_integrity=not_found)
        return cur.rowcount == 1

    def retire_public_aliases(self, kind: AliasKind, source: AliasSource, keep: Set[Tuple[str, str]]) -> int:
        """删掉某类来源的公共别名里不在 keep（信用代码，规范写法的别名）里的行，让离线清单成为这一类别名的唯一依据。返回删除的行数。"""
        rows = self._execute("SELECT alias_id, uscc, alias FROM company_aliases WHERE alias_kind = %s AND source = %s",
                             (kind.value, source.value)).fetchall()
        stale = [r[0] for r in rows if (r[1], normalize_name(r[2])) not in keep]
        for alias_id in stale:
            self._execute("DELETE FROM company_aliases WHERE alias_id = %s", (alias_id,))
        return len(stale)

    def confirm_alias(self, alias: str, uscc: str, session_id: Optional[str] = None) -> None:
        """用户确认某称呼就是某家公司。同一称呼再次确认覆盖原行。"""
        if not isinstance(alias, str) or not alias.strip():
            raise EvaError(Code.INVALID_VALUE, "称呼为空")
        alias = normalize_name(alias)
        if session_id is not None and not self._execute(
            "SELECT 1 FROM sessions WHERE session_id = %s AND tenant_id = %s", (session_id, self.tenant_id)
        ).fetchone():
            raise EvaError(Code.SESSION_NOT_FOUND, "会话不存在", session_id=session_id)
        not_found = {FK_VIOLATION: EvaError(Code.COMPANY_NOT_FOUND, "公司不在主数据里", uscc=uscc)}
        cur = self._execute(
            "UPDATE tenant_aliases SET uscc = %s, session_id = %s WHERE tenant_id = %s AND alias = %s",
            (uscc, session_id, self.tenant_id, alias),
            on_integrity=not_found,
        )
        if cur.rowcount == 0:
            self._execute(
                "INSERT INTO tenant_aliases (tenant_id, alias, uscc, session_id, created_at) VALUES (%s, %s, %s, %s, %s)",
                (self.tenant_id, alias, uscc, session_id, clock.now()),
                on_integrity=not_found,
            )
