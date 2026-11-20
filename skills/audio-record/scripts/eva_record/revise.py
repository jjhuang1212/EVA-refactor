"""用户看了修改清单之后的修订：全部应用、按 id 取舍、改了清单发回来、口述改某处，或只补充标色实体。
从修订单重放：用原始转写和改动表重新过一遍闸门，用户确认的改动（by=user）只查位置，然后写出新的一套文件。"""
import json
import os
from datetime import datetime

from eva_asr import apply_changes

from eva_record import changes as ch
from eva_record import companies as co
from eva_record.build import CST, _write, emit
from eva_record.errors import RecordError

_KEYS = ("source", "scenario", "info", "speakers", "entities", "materials", "blocks", "changes")
_SUBMIT = ("block", "blocks", "speaker", "old", "new", "kind", "reason", "by", "all", "parts", "moved")


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError) as e:
        raise RecordError(f"读不到修订单：{e}") from e
    if not isinstance(state, dict) or any(k not in state for k in _KEYS):
        raise RecordError(f"修订单格式不对，缺 {[k for k in _KEYS if k not in state] if isinstance(state, dict) else '全部'}")
    return state


def _new_row(rows, a):
    if not (isinstance(a, dict) and isinstance(a.get("block"), int) and isinstance(a.get("old"), str) and a["old"]
            and isinstance(a.get("new"), str)):
        raise RecordError(f"新增的改动要有 block（整数）、old（非空）、new：{a!r}")
    kind = a.get("kind", "fix")
    return {"id": f"c{len(rows) + 1}", "stage": 1 if kind == "tidy" else 2, "block": a["block"], "old": a["old"], "new": a["new"],
            "kind": kind, "reason": a.get("reason", "用户指定"), "by": "user", **({"all": True} if a.get("all") else {}),
            "status": ch.PENDING, "why": ""}


def revise(state, decisions, entities=None, now=None, config=None, outdir=".", companies=None):
    """state 是修订单（dict 或 .json 路径）。decisions 是 {"apply": "all" 或 [id], "skip": [id], "edit": {id: 改后},
    "add": [{"block"(可选), "old", "new", "all"(可选)}]}，用户发回修改清单时用 parse_change_list 得到。
    add 不写 block 是用户口述“把 X 改成 Y”没说在哪：整理后的转写里每个出现 X 的块、每一处都改；一处都找不到的列在 failed 里。
    entities 给了就补进标色实体（补标漏标的公司、用户说“把X上色”时用，原来标的保留）；companies 给了就替换"涉及公司"表（用户说"X 是 Y 公司"时用）。只写记录 PDF 和修订单（见 emit 的 final_only）。
    返回 emit 的结果，另有 failed：用户指定却没能应用的改动及原因。"""
    state = state if isinstance(state, dict) else load_state(state)
    rows = [dict(c) for c in state["changes"]]
    by_id = {c["id"]: c for c in rows}
    d = {"apply": [], "skip": [], "edit": {}, "add": [], **decisions}
    picked = [] if d["apply"] == "all" else list(d["apply"])
    unknown = [i for i in (*picked, *d["skip"], *d["edit"]) if i not in by_id]
    if unknown:
        raise RecordError(f"修订单里没有这些 id：{'、'.join(map(str, unknown))}")
    want = {c["id"] for c in rows if c["status"] == ch.APPLIED}
    if d["apply"] == "all":
        want |= {c["id"] for c in rows if c["status"] == ch.PENDING}
    want |= set(picked) | set(d["edit"])
    want -= set(d["skip"])
    for i, new in d["edit"].items():
        by_id[i]["new"] = new
        by_id[i]["by"] = "user"
    for c in rows:
        if c["id"] in d["skip"]:
            c.update(status=ch.DROPPED, why="")
        elif c["id"] in want and c["status"] != ch.APPLIED:
            c["by"] = "user"
    for a in d["add"]:
        if not (isinstance(a, dict) and a.get("block") is None):
            rows.append(_new_row(rows, a))
            want.add(rows[-1]["id"])
    transcript = {"source": state["source"], "blocks": [dict(b) for b in state["blocks"]]}
    stage1_rows = [c for c in rows if c["stage"] == 1 and c["id"] in want]
    stage1 = apply_changes(transcript, [{k: c[k] for k in _SUBMIT if k in c} for c in stage1_rows])

    def second(picked):
        return apply_changes(stage1, [{k: c[k] for k in _SUBMIT if k in c} for c in picked])

    missing = []
    prior = second([c for c in rows if c["stage"] == 2 and c["id"] in want])  # 口述的改动要找的是校订、拆块之后的文字
    for a in (a for a in d["add"] if isinstance(a, dict) and a.get("block") is None):
        _new_row(rows, {**a, "block": 0})  # 只为校验 old、new 的格式
        hits = [b["ord"] for b in prior["blocks"] if a["old"] in b["text"]]
        missing += [] if hits else [f"「{a['old']}」在整理后的转写里找不到，没有改"]
        for block in hits:
            rows.append(_new_row(rows, {**a, "block": block, "all": True, "kind": "fix"}))
            want.add(rows[-1]["id"])
    stage2_rows = [c for c in rows if c["stage"] == 2 and c["id"] in want]
    stage2 = second(stage2_rows)
    for picks, result in ((stage1_rows, stage1), (stage2_rows, stage2)):
        for a in result["applied"]:
            picks[a["index"]].update(status=ch.APPLIED, why=a.get("basis", ""))
        for r in result["rejected"]:
            c = picks[r["index"]]
            c.update(status=ch.FAILED if c.get("by") == "user" else (ch.PENDING if r["applicable"] else ch.BLOCKED), why=r["reason"])
    ctx = {**{k: state[k] for k in ("source", "scenario", "info", "speakers", "entities", "materials")},
           **({"entities": [list(e) for e in dict.fromkeys(tuple(e) for e in [*state["entities"], *entities])]} if entities is not None else {}),
           "companies": state.get("companies", []) if companies is None else companies}
    out = emit(stage1, stage2, rows, ctx, now, config, outdir, final_only=True)
    failed = missing + [f"{c['id']}（块 {c['block']}「{c['old']}」）：{c['why']}" for c in rows if c["status"] == ch.FAILED]
    said = [f"「{c['old']}」在记录里找不到" for c in rows if c["status"] == ch.FAILED] + [m for m in missing]
    return {**out, "failed": failed, "message": co.message(rows, out["confirmations"], said, revised=True)}


def change_list(state, now=None, outdir="."):
    """用户问"你改了哪些地方"时，由修订单生成对照清单 md（不改任何内容），返回 {"changes": 路径}。线上默认不主动发，用户要了才给。"""
    state = state if isinstance(state, dict) else load_state(state)
    stem = os.path.splitext(os.path.basename(state["source"]))[0]
    path = os.path.join(outdir, f"{stem}-修改清单-{(now or datetime.now(CST)):%Y%m%d-%H%M%S}.md")
    os.makedirs(outdir, exist_ok=True)
    _write(path, ch.to_markdown(stem, state["changes"], state.get("final", [])))
    return {"changes": path}


def parse_change_list(text, state):
    """读用户改过的修改清单 md，得到 revise 用的 decisions。state 是修订单（dict 或路径）。"""
    state = state if isinstance(state, dict) else load_state(state)
    return ch.parse_markdown(text, state["changes"])
