"""旧库 ccm 公共数据一次性迁入 eva_perf。

只补缺：目标里已有的行不覆盖（上线后公司主数据会被 QCC 回填，不能被重跑抹掉）。
与 company_master 名称字段文字相同的 SHORT / FULL 别名是派生数据，不迁。
旧库只发 SELECT。用法：python -m eva.storage.legacy_import [--unmatched-out 路径]
"""
import argparse
import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set

import pymssql

from eva.common import clock
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import AliasKind, AliasSource, HotwordSource, check
from eva.storage import db

USCC_LEN = 18
DEFAULT_UNMATCHED_CSV = Path(__file__).resolve().parents[2] / "docs" / "迁移未匹配上市公司.csv"

SOURCE_QUERIES = {
    "companies": "SELECT company_id, full_name, short_name, sec_code, exchange, listed_at, delisted_at, updated_at "
                 "FROM companies",
    # 是否与主表同名字段相同由数据库按其排序规则判断（忽略大小写、全半角），与 0002 迁移里的删除条件一致
    "aliases": "SELECT a.company_id, a.alias, a.alias_kind, "
               "CASE WHEN (a.alias_kind = 'FULL' AND a.alias = c.full_name) "
               "OR (a.alias_kind = 'SHORT' AND a.alias = c.short_name) THEN 1 ELSE 0 END AS is_derived "
               "FROM company_aliases a LEFT JOIN companies c ON c.company_id = a.company_id",
    "hotwords": "SELECT term, weight, source_kind, company_id, industry, created_at FROM asr_hotwords",
    "registry": "SELECT exchange, sec_code, short_name, full_name, listed_at, delisted_at, status "
                "FROM listed_company_registry",
}

_INSERT_COMPANY = (
    "INSERT INTO company_master (uscc, full_name, short_name, sec_code, exchange, listed_at, delisted_at, "
    "data_json, fetched_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, %s, %s)"
)
_INSERT_ALIAS = (
    "INSERT INTO company_aliases (uscc, alias, alias_kind, source, created_at) VALUES (%s, %s, %s, %s, %s)"
)
_INSERT_HOTWORD = (
    "INSERT INTO asr_hotwords (term, weight, source_kind, company_id, industry, created_at) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)


@dataclass
class Plan:
    companies: List[dict] = field(default_factory=list)  # 待插入
    aliases: List[dict] = field(default_factory=list)
    hotwords: List[dict] = field(default_factory=list)
    source_valid: Dict[str, int] = field(default_factory=dict)  # 通过校验的源行数
    already_present: Dict[str, int] = field(default_factory=dict)
    exceptions: List[tuple] = field(default_factory=list)  # (表, 键, 原因)
    registry_unmatched: List[dict] = field(default_factory=list)
    registry_inconsistent: List[dict] = field(default_factory=list)
    registry_matched: int = 0
    derived_skipped: int = 0  # 与主表同名字段相同的 SHORT / FULL 别名，不落表
    valid_keys: Dict[str, Set[tuple]] = field(default_factory=dict)  # 对账用：通过校验的源行在目标里必须存在


def build_plan(src: Dict[str, List[dict]], existing: Dict[str, Set[tuple]]) -> Plan:
    """纯函数：决定哪些源行要插入、哪些是异常、注册表怎么对账。"""
    p = Plan()

    valid_companies = {}
    for r in src["companies"]:
        if r["company_id"] is None or len(r["company_id"]) != USCC_LEN:
            p.exceptions.append(("companies", r["company_id"], "uscc 不是 18 位"))
        else:
            valid_companies[r["company_id"]] = r
    p.source_valid["companies"] = len(valid_companies)
    p.valid_keys["companies"] = {(k,) for k in valid_companies}
    p.companies = [r for k, r in valid_companies.items() if (k,) not in existing["companies"]]
    p.already_present["companies"] = len(valid_companies) - len(p.companies)

    valid_aliases = []
    for r in src["aliases"]:
        kind = check(AliasKind, r["alias_kind"])
        owner = valid_companies.get(r["company_id"])
        if owner is None:
            p.exceptions.append(("company_aliases", (r["company_id"], r["alias"]), "所属公司不在可迁移的公司里"))
        elif r["is_derived"]:
            p.derived_skipped += 1
        else:
            valid_aliases.append({**r, "alias_kind": kind.value})
    p.source_valid["aliases"] = len(valid_aliases)
    p.valid_keys["aliases"] = {(r["company_id"], r["alias"], r["alias_kind"]) for r in valid_aliases}
    p.aliases = [r for r in valid_aliases if (r["company_id"], r["alias"], r["alias_kind"]) not in existing["aliases"]]
    p.already_present["aliases"] = len(valid_aliases) - len(p.aliases)

    valid_hotwords = []
    for r in src["hotwords"]:
        kind = check(HotwordSource, r["source_kind"])
        if r["company_id"] is not None and len(r["company_id"]) != USCC_LEN:
            p.exceptions.append(("asr_hotwords", r["term"], "company_id 不是 18 位"))
        else:
            valid_hotwords.append({**r, "source_kind": kind.value})
    p.source_valid["hotwords"] = len(valid_hotwords)
    p.valid_keys["hotwords"] = {(r["term"], r["source_kind"]) for r in valid_hotwords}
    p.hotwords = [r for r in valid_hotwords if (r["term"], r["source_kind"]) not in existing["hotwords"]]
    p.already_present["hotwords"] = len(valid_hotwords) - len(p.hotwords)

    by_listing = {(r["exchange"], r["sec_code"]): r for r in src["companies"] if r["sec_code"] and r["exchange"]}
    for r in src["registry"]:
        c = by_listing.get((r["exchange"], r["sec_code"]))
        if c is None:
            p.registry_unmatched.append(r)
            continue
        p.registry_matched += 1
        delisted = r["status"] == "DELISTED"
        if (c["listed_at"], c["delisted_at"]) != (r["listed_at"], r["delisted_at"]) or delisted != (
            c["delisted_at"] is not None
        ):
            p.registry_inconsistent.append(r)
    return p


def _read_source(src_conn) -> Dict[str, List[dict]]:
    cur = src_conn.cursor(as_dict=True)
    out = {}
    for name, sql in SOURCE_QUERIES.items():
        cur.execute(sql)
        out[name] = cur.fetchall()
    return out


def _target_keys(cur) -> Dict[str, Set[tuple]]:
    queries = {
        "companies": "SELECT uscc FROM company_master",
        "aliases": "SELECT uscc, alias, alias_kind FROM company_aliases",
        "hotwords": "SELECT term, source_kind FROM asr_hotwords",
    }
    out = {}
    for name, sql in queries.items():
        cur.execute(sql)
        out[name] = {tuple(row) for row in cur.fetchall()}
    return out


def import_public(src_conn, dst_conn) -> Plan:
    """在 dst_conn 的当前事务里补缺并对账，不提交；对账不平抛 E9004。"""
    try:
        src = _read_source(src_conn)
        cur = dst_conn.cursor()
        plan = build_plan(src, _target_keys(cur))

        now = clock.now()
        cur.executemany(
            _INSERT_COMPANY,
            [
                (r["company_id"], r["full_name"], r["short_name"], r["sec_code"], r["exchange"],
                 r["listed_at"], r["delisted_at"], r["updated_at"], r["updated_at"])
                for r in plan.companies
            ],
        )
        cur.executemany(
            _INSERT_ALIAS,
            [(r["company_id"], r["alias"], r["alias_kind"], AliasSource.LEGACY.value, now) for r in plan.aliases],
        )
        cur.executemany(
            _INSERT_HOTWORD,
            [(r["term"], r["weight"], r["source_kind"], r["company_id"], r["industry"], r["created_at"])
             for r in plan.hotwords],
        )
        _reconcile(plan, _target_keys(cur))
        return plan
    except pymssql.Error as e:
        raise db.translate(e) from e


def _reconcile(plan: Plan, target: Dict[str, Set[tuple]]) -> None:
    """源里通过校验的每一行，目标里都必须有。"""
    missing = {name: sorted(keys - target[name])[:5] for name, keys in plan.valid_keys.items() if keys - target[name]}
    if missing:
        raise EvaError(Code.RECONCILE_MISMATCH, "源数据未全部落到目标", sample_missing=missing)


def write_unmatched_csv(plan: Plan, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["exchange", "sec_code", "short_name", "full_name", "listed_at", "delisted_at", "status"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(plan.registry_unmatched)


def summary(plan: Plan) -> str:
    lines = []
    for name in ("companies", "aliases", "hotwords"):
        inserted = len(getattr(plan, name))
        lines.append(f"{name}: 通过校验 {plan.source_valid[name]}，新插入 {inserted}，目标已有 {plan.already_present[name]}")
    lines.append(f"别名中与主表名称相同、不落表的 SHORT / FULL: {plan.derived_skipped}")
    lines.append(f"异常行 {len(plan.exceptions)}")
    lines += [f"  {t} {k} {why}" for t, k, why in plan.exceptions[:20]]
    lines.append(
        f"上市注册表: 匹配 {plan.registry_matched}，未匹配 {len(plan.registry_unmatched)}，"
        f"匹配但上市信息不一致 {len(plan.registry_inconsistent)}"
    )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--unmatched-out", type=Path, default=DEFAULT_UNMATCHED_CSV)
    args = ap.parse_args()

    src_conn, dst_conn = db.connect_legacy(), db.connect()
    try:
        plan = import_public(src_conn, dst_conn)
        dst_conn.commit()
    except BaseException:
        dst_conn.rollback()
        raise
    finally:
        src_conn.close()
        dst_conn.close()
    write_unmatched_csv(plan, args.unmatched_out)
    print(summary(plan))
    print(f"未匹配清单: {args.unmatched_out}")


if __name__ == "__main__":
    main()
