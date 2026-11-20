"""对比同一案例的两组运行（按 --label 选组）：通过率、步数和费用的范围、每条断言的通过情况、代码版本。

只读 out/<案例>/*/result.json，不重跑、不花钱。用法：python -m devtools.eval.compare <案例名> <标签 A> <标签 B>
"""
import argparse
import json
import statistics
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

from devtools.eval import snapshot
from devtools.eval.runner import INPUTS_DIR, OUT
from eva.contracts.errors import Code, EvaError
from eva.storage import db
from eva.storage.developer import Developer


_STATUS_ZH = {"added": "新增", "removed": "删除", "changed": "改动"}


def load_runs(case_name: str, label: str, out_root: Path = OUT) -> List[dict]:
    """案例下带这个标签的所有运行。没有 result.json 的目录（运行中途崩溃）没有标签可认，不在内。"""
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((out_root / case_name).glob("*/result.json"))]
    runs = [r for r in runs if r.get("label") == label]
    if not runs:
        raise EvaError(Code.FILE_NOT_FOUND, "没有这个标签的运行", case=case_name, label=label)
    return runs


def _range(values: list) -> dict:
    return {"min": min(values), "median": statistics.median(values), "max": max(values)} if values else None


def summarize(runs: List[dict], human: Optional[Dict[str, str]] = None) -> dict:
    """human 是 run_id 到人工判定（pass/fail）；None 表示不看人工标注。"""
    asserts = {}
    for r in runs:
        for a in r["asserts"]:
            got = asserts.setdefault(a["name"], [0, 0])
            got[0] += a["ok"]
            got[1] += 1
    return {
        "runs": len(runs),
        "passed": sum(r["passed"] for r in runs),
        "ended": dict(Counter(r["error"]["code"] if r["error"] else r["reason"] for r in runs)),
        "steps": _range([r["steps"] for r in runs if r["steps"] is not None]),
        "cost_cny": _range([Decimal(r["cost"]["total_cny"]) for r in runs]),
        "code_versions": sorted({str(r["code_version"]) for r in runs}),
        "inputs": dict(Counter(r.get("inputs_sha") for r in runs)),  # 没有记录的早期运行是 None
        "asserts": asserts,
        "human": None if human is None else {
            "pass": sum(human.get(r["run_id"]) == "pass" for r in runs),
            "fail": sum(human.get(r["run_id"]) == "fail" for r in runs),
            "unmarked": sum(r["run_id"] not in human for r in runs)},
    }


def _main_inputs(s: dict):
    """一组里用得最多的那一版输入；全都没记录则是 None。"""
    known = {k: v for k, v in s["inputs"].items() if k is not None}
    return max(known, key=known.get) if known else None


def _inputs_diff(a: dict, b: dict, out_root: Path) -> dict:
    sa, sb = _main_inputs(a), _main_inputs(b)
    if sa is None or sb is None:
        return {"state": "unrecorded", "files": []}
    if sa == sb:
        return {"state": "same", "files": []}
    store = out_root / INPUTS_DIR
    return {"state": "different", "a": sa, "b": sb, "files": snapshot.diff(snapshot.load(sa, store), snapshot.load(sb, store))}


def compare(case_name: str, label_a: str, label_b: str, out_root: Path = OUT,
            human: Optional[Dict[str, str]] = None) -> dict:
    a = summarize(load_runs(case_name, label_a, out_root), human)
    b = summarize(load_runs(case_name, label_b, out_root), human)
    names = sorted(set(a["asserts"]) | set(b["asserts"]))
    return {"case": case_name, "a": {"label": label_a, **a}, "b": {"label": label_b, **b},
            "inputs_diff": _inputs_diff(a, b, out_root),
            "asserts": [{"name": n, "a": a["asserts"].get(n), "b": b["asserts"].get(n)} for n in names]}


def _frac(v) -> str:
    return "无" if v is None else f"{v[0]}/{v[1]}"


def _rng(r) -> str:
    return "无" if r is None else f"{r['min']} / {r['median']} / {r['max']}"


def render(c: dict) -> str:
    a, b = c["a"], c["b"]
    lines = [f"案例 {c['case']}：{a['label']} → {b['label']}", f"{'':14}{a['label']:<26}{b['label']}"]
    for title, fa, fb in (
            ("运行次数", a["runs"], b["runs"]),
            ("通过", f"{a['passed']}/{a['runs']}", f"{b['passed']}/{b['runs']}"),
            ("步数 小/中/大", _rng(a["steps"]), _rng(b["steps"])),
            ("费用 小/中/大", _rng(a["cost_cny"]), _rng(b["cost_cny"])),
            ("结束原因", a["ended"], b["ended"]),
            ("代码版本", "、".join(a["code_versions"]), "、".join(b["code_versions"]))):
        lines.append(f"{title:<14}{str(fa):<26}{fb}")
    if a["human"] is not None and b["human"] is not None:  # 通过/不通过/未标
        ga, gb = (f"{h['pass']}/{h['fail']}/{h['unmarked']}" for h in (a["human"], b["human"]))
        lines.append(f"{'人工判定':<14}{ga:<26}{gb}")
    lines.append("断言（通过次数/运行次数）：")
    for x in c["asserts"]:
        pa, pb = (x[k] and x[k][0] / x[k][1] for k in ("a", "b"))
        mark = "" if pa == pb else "  ← 变化"
        lines.append(f"  {x['name']:<30}{_frac(x['a']):<10}{_frac(x['b'])}{mark}")
    d = c["inputs_diff"]
    for g in (a, b):
        if len([k for k in g["inputs"] if k is not None]) > 1:
            lines.append(f"警告：组 {g['label']} 里混了多版输入，对比可能不干净（按用得最多的一版比较）。")
    if d["state"] == "same":
        lines.append("输入（提示词、skill、配置）两组完全相同，差别只来自模型每次的随机性。")
    elif d["state"] == "different":
        lines.append(f"输入有差异（{d['a'][:8]} → {d['b'][:8]}），共 {len(d['files'])} 个文件：")
        for f in d["files"]:
            lines.append(f"  [{_STATUS_ZH[f['status']]}] {f['path']}")
            lines.extend("    " + ln.rstrip("\n") for ln in (f["diff"] or ["（不是文本文件，内容已变）"]))
    else:
        lines.append("有一组没有记录输入，无法比较提示词、skill、配置的差异。")
        if a["code_versions"] == b["code_versions"]:
            lines.append("提示：两组的代码版本相同，差别可能只来自模型每次的随机性（带 +dirty 的版本内容可能不同）。")
    return "\n".join(lines)


def human_verdicts(case_name: str) -> Dict[str, str]:
    """案例下每次运行当前生效的人工判定（来自数据库）。库连不上就抛错，不当成"没人标过"。"""
    with db.transaction() as conn:
        return {r["run_id"]: r["verdict"] for r in Developer(conn).annotations(case_name=case_name)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m devtools.eval.compare")
    ap.add_argument("case")
    ap.add_argument("label_a")
    ap.add_argument("label_b")
    args = ap.parse_args(argv)
    try:
        load_dotenv()
        print(render(compare(args.case, args.label_a, args.label_b, human=human_verdicts(args.case))))
    except EvaError as e:
        print(f"{e} {e.details or ''}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
