"""把 tools/group_aliases.yaml 里的集团叫法导入公共别名（company_aliases，kind=GROUP，source=offline）。

离线整理用，不是租户请求：不记账，QCC 的搜索费由跑这个脚本的人承担（每个本地没有的全称 0.10 元，数量在最后打印）。
每个叫法只指向清单里的 primary 一家：先查本地主数据，没有再问 QCC，名字和清单里的全称完全一致才采用，对不上的跳过并报出来，不猜。
冲突检查：叫法如果已经是别的公司的全称、简称或别的来源的公共别名，或者清单里两个集团用了同一个叫法，整批都不写，报出来由人改清单。
幂等：已有的别名不重复写，清单里没有的旧 GROUP 别名会被清理（有主体没对上时不清理，免得误删）。
用法：python -m tools.seed_group_aliases
"""
import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import yaml
from dotenv import load_dotenv

from eva.contracts.vocab import AliasKind, AliasSource
from eva.storage import db
from eva.storage.companies import Companies, normalize_name
from eva.tools import config as tools_config
from eva.tools.qcc import Qcc

LIST_PATH = Path(__file__).with_name("group_aliases.yaml")
_HIT_TEXT = {"full_name": "全称", "short_name": "简称", "alias": "别名"}
OFFLINE_TENANT = "offline"  # Companies 要求绑定租户，导入不属于任何租户，这里只用它的公共表方法


def find_conflicts(repo: Companies, wanted: List[Tuple[str, str]]) -> List[str]:
    """wanted 是（叫法，信用代码）列表。返回人能读的冲突说明；空表示没有冲突。"""
    problems, seen = [], {}
    for alias, uscc in wanted:
        key = normalize_name(alias)
        if seen.setdefault(key, uscc) != uscc:
            problems.append(f"「{alias}」在清单里同时指向两家公司（{seen[key]}、{uscc}）")
        for other in repo.alias_conflicts(alias, uscc):
            problems.append(f"「{alias}」已经是 {other['full_name']}（{other['uscc']}）的{_HIT_TEXT[other['matched_by']]}")
    return problems


def main() -> int:
    load_dotenv(".env")
    cfg = tools_config.load_qcc()
    qcc = Qcc(cfg, os.environ["QCC_API_KEY"])
    groups = yaml.safe_load(LIST_PATH.read_text(encoding="utf-8"))
    paid, skipped, written, retired, wanted = 0, [], 0, 0, []
    resolved: Dict[str, str] = {}
    conn = db.connect()
    try:
        repo = Companies(conn, OFFLINE_TENANT)
        for group in groups:
            full = group["primary"]
            local = [m for m in repo.resolve(full) if m["matched_by"] == "full_name"]
            if local:
                resolved[full] = local[0]["uscc"]
            else:
                paid += 1
                hits = [h for h in qcc.search(normalize_name(full)).hits if normalize_name(h.name) == normalize_name(full)]
                if not hits:
                    skipped.append(full)
                    continue
                repo.record_qcc(hits[0].uscc, hits[0].name, hits[0].raw)
                resolved[full] = hits[0].uscc
            wanted += [(alias, resolved[full]) for alias in group["aliases"]]
        problems = find_conflicts(repo, wanted)
        if problems:
            conn.rollback()
            print("叫法有冲突，本次什么都没有写，请改清单后重跑：")
            for p in problems:
                print("  -", p)
            return 1
        for alias, uscc in wanted:
            written += repo.add_public_alias(uscc, alias, AliasKind.GROUP, AliasSource.OFFLINE)
        if not skipped:
            retired = repo.retire_public_aliases(AliasKind.GROUP, AliasSource.OFFLINE, {(u, normalize_name(a)) for a, u in wanted})
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    for group in groups:
        print(f"{'/'.join(group['aliases'])} -> {group['primary']}" + ("（没对上，已跳过）" if group["primary"] in skipped else ""))
    if skipped:
        print("有主体没对上，为免误删，本次没有清理清单之外的旧别名")
    print(f"新写入别名 {written} 条，清理清单之外的旧别名 {retired} 条；向 QCC 搜索 {paid} 次（约 {paid * float(cfg.search_price_cny):.2f} 元）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
