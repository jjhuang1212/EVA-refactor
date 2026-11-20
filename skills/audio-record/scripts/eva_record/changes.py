"""修订单：把两轮闸门的结果合成一张带 id 和状态的改动表，写成用户能改的修改清单 md，也把用户改过的 md 读回来。

状态：已应用、待应用（被闸门拦下，但用户确认后可以应用）、不可应用（位置或格式有问题）、不应用（用户撤销或放弃）、
应用失败（用户指定的改动在文本里对不上）。
"""
import re

from eva_asr import KIND_LABELS, UNLISTED
from eva_asr.diff_gate import numbers

from eva_record.errors import RecordError

APPLIED, PENDING, BLOCKED, DROPPED, FAILED = "已应用", "待应用", "不可应用", "不应用", "应用失败"
STATUSES = (APPLIED, PENDING, BLOCKED, DROPPED, FAILED)
SECTIONS = {1: "原始转录整理", 2: "上下文纠错"}
DELETED = "（删除）"
HEAD = ["id", "块", "类型", "原文", "改后", "状态", "说明"]
NOTE = ("本清单可以改了发回来重新出 PDF：待应用的行把“状态”改成“应用”就改上；已应用的行把“状态”改成“不应用”（或删掉这一行）就撤回，"
        "文字回到改之前的样子；直接改“改后”那一格（原文不要改）也行；想加一处，新增一行，id 留空，写块号、原文、改后。"
        "也可以直接说“全部应用”或“应用 c3、c5”。没有逐条列出的（整理、错别字纠正、拆分、合并）表里撤回不了，直接告诉我“把X改回Y”。")
_SENTENCE_END = re.compile(r"[。？！?!]")
_LABEL = re.compile(r"\A[A-Za-z0-9.]+：")
_SPLIT = re.compile(r"(?<!\\)\|")
_ID = re.compile(r"c\d+")


def collect(stage1, stage2):
    """两轮结果按提交顺序合成改动表。每条：id、stage、block、old、new、kind、reason、status、why，另有 by、all、moved、basis（有才带）。"""
    out = []
    for stage, result in ((1, stage1), (2, stage2)):
        rows = [(a["index"], a, None) for a in result["applied"]] + [(r["index"], r["change"], r) for r in result["rejected"]]
        for _, ch, rejected in sorted(rows, key=lambda x: x[0]):
            if rejected is not None and rejected["code"] in UNLISTED:
                continue  # 改写、放错轮次的不列给用户；模型在 apply_changes 的返回里已经看到原因
            ch = ch if isinstance(ch, dict) else {"old": repr(ch)}
            split = ch.get("kind") == "split" and isinstance(ch.get("parts"), (list, tuple))
            old, new = (("".join(t for _, t in ch["parts"]), " ｜ ".join(f"{s}：{t}" for s, t in ch["parts"])) if split else (ch.get("old"), ch.get("new")))
            row = {"id": f"c{len(out) + 1}", "stage": stage, "block": (ch.get("blocks") or [None])[0] if ch.get("kind") == "merge" else ch.get("block"), "old": old, "new": new,
                   "kind": ch.get("kind", "fix"), "reason": ch.get("reason", ""),
                   **{k: ch[k] for k in ("by", "all", "parts", "blocks", "speaker", "moved") if k in ch}}
            if rejected is None:
                row.update(status=APPLIED, why=ch.get("basis", ""))
            else:
                row.update(status=PENDING if rejected["applicable"] else BLOCKED, why=rejected["reason"])
            out.append(row)
    return out


def counts(changes):
    return "，".join(f"{s} {n} 处" for s in STATUSES if (n := sum(1 for c in changes if c["status"] == s)))


def _cell(text):
    return ("" if text is None else str(text)).replace("\n", " ").replace("|", "\\|")


def _notes(c):
    return "；".join(x for x in (c.get("reason"), c["why"], "用户确认" if c.get("by") == "user" and c["status"] == APPLIED else "") if x)


def folded(c):
    """已应用的整理、数字没变的错别字纠正、拆分、合并，以及整句挪到相邻块的改动，数量大、用户不会逐条看，清单里只给个数，不列行。
    用户自己指定的、改说话人、数字更正不折叠。"""
    if c["status"] != APPLIED or c.get("by") == "user":
        return False
    if c["kind"] in ("tidy", "split", "merge"):
        return True
    return c["kind"] == "fix" and (bool(c.get("moved")) or numbers(c["old"] or "") == numbers(c["new"] or ""))


def sentences(old, new, bodies, span=40):
    """old 所在的那一句（以。？！为界）和把 old 换成 new 之后的同一句，供用户一眼看懂改的是哪里；句子太长只留改动前后各 span 个字。
    bodies 是稿子里每个块的文字。找不到 old 返回 None。"""
    for text in bodies:
        at = text.find(old)
        if at < 0:
            continue
        lead = len(old) - len(old.lstrip("。？！?!"))  # 片段开头带着上一句的句号，改前改后共有的这部分不算在这一句里
        if lead and new.startswith(old[:lead]):
            old, new, at = old[lead:], new[lead:], at + lead
        start = max((m.end() for m in _SENTENCE_END.finditer(text, 0, at)), default=0)
        tail = _SENTENCE_END.search(text, at + len(old))
        end = tail.end() if tail else len(text)
        if start == 0 and (lab := _LABEL.match(text)):
            start = lab.end()
        lo, hi = max(start, at - span), min(end, at + len(old) + span)
        pre, post = ("…" if lo > start else ""), ("…" if hi < end else "")
        return (pre + text[lo:at] + old + text[at + len(old):hi] + post,
                pre + text[lo:at] + new + text[at + len(old):hi] + post)
    return None


def _context(c, bodies):
    """“原句／改后”两句。已应用的在稿子里找改后的写法，其余的找原文。"""
    if c["kind"] in ("split", "merge", "relabel"):
        return ""
    applied = c["status"] == APPLIED
    here, other = (c["new"], c["old"]) if applied else (c["old"], c["new"])
    found = sentences(here, other, bodies) if here else None
    if found is None:
        return ""
    before, after = (found[1], found[0]) if applied else found
    return f"原句：{before}；改后：{after}"


def to_markdown(stem, changes, bodies=()):
    lines = [f"# {stem} 修改清单", "", NOTE, ""]
    for stage, title in SECTIONS.items():
        all_rows = [c for c in changes if c["stage"] == stage]
        rows = [c for c in all_rows if not folded(c)]
        hidden = len(all_rows) - len(rows)
        lines += [f"## {title}", "", (counts(all_rows) or "没有改动") + "。" + (f"其中 {hidden} 处已应用的整理（语气词、口吃重复等）、错别字纠正、拆分和合并不逐条列出。" if hidden else ""), ""]
        if rows:
            lines += ["| " + " | ".join(HEAD) + " |", "|" + "---|" * len(HEAD)]
            lines += [f"| {c['id']} | b{c['block']} | {KIND_LABELS.get(c['kind'], '纠错')} | {_cell(c['old'])} | {_cell(c['new']) or DELETED} "
                      f"| {c['status']} | {_cell('；'.join(x for x in (_notes(c), _context(c, bodies)) if x))} |" for c in rows]
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _row(line):
    cells = [c.strip().replace("\\|", "|") for c in _SPLIT.split(line.strip()[1:-1] if line.strip().endswith("|") else line.strip()[1:])]
    if len(cells) != len(HEAD):
        raise RecordError(f"修改清单这一行的格数不对（应有 {len(HEAD)} 格）：{line.strip()[:50]}")
    return cells


def _block(cell):
    m = re.fullmatch(r"b?(\d+)", cell)
    if not m:
        raise RecordError(f"块号要写成 b12 这样：{cell!r}")
    return int(m.group(1))


def parse_markdown(text, changes):
    """读用户改过的修改清单，得到修订指令 {"apply": [id], "skip": [id], "edit": {id: 改后}, "add": [{block, old, new, kind}]}。
    原文被改、块号被改、id 不在上一版里，都报错，不猜。表里没有的已应用行算撤销（折叠掉的整理不在表里，不算）。"""
    by_id = {c["id"]: c for c in changes}
    decisions, seen, rows = {"apply": [], "skip": [], "edit": {}, "add": []}, set(), 0
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = _row(s)
        if cells[0] == HEAD[0] or all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        rid, blk, kind, old, new, status = cells[:6]
        new = "" if new == DELETED else new
        rows += 1
        if not rid:
            if status != "不应用":
                if not old:
                    raise RecordError(f"新增的一行要写原文：{s[:50]}")
                decisions["add"].append({"block": _block(blk), "old": old, "new": new, "kind": "tidy" if "整理" in kind else "fix"})
            continue
        c = by_id.get(rid)
        if not _ID.fullmatch(rid) or c is None:
            raise RecordError(f"{rid!r} 不在上一版修订单里。新增的行 id 请留空")
        seen.add(rid)
        if _block(blk) != c["block"] or old != (c["old"] or "").replace("\n", " "):
            raise RecordError(f"{rid} 的块号或原文被改过。想改别处请新增一行（id 留空），想改结果请改“改后”")
        if status == "不应用":
            decisions["skip"].append(rid)
        elif status == "应用":
            decisions["apply"].append(rid)
        if new != (c["new"] or "").replace("\n", " ") and status != "不应用":
            if c["kind"] in ("split", "merge", "relabel"):
                raise RecordError(f"{rid} 是{KIND_LABELS[c['kind']]}，只能改“状态”，不能改“改后”")
            decisions["edit"][rid] = new
    if not rows:
        raise RecordError("没读到修改清单的表格，请确认发回的是修改清单")
    decisions["skip"] += [c["id"] for c in changes if c["id"] not in seen and c["status"] == APPLIED and not folded(c)]
    return decisions
