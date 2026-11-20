"""改动清单的闸门。清单里每条形如 {"block": 块号, "old": 原文片段, "new": 改后, "reason": 依据, "kind": "fix"、"tidy" 或 "number_fix"}。
结构上的改动三种：拆块 {"kind": "split", "block": 块号, "parts": [[说话人标签, 这一段原文], ...]}（几段都行，拼起来必须正好是原块的文字）；
合并 {"kind": "merge", "blocks": [块号, ...]}（同一人说的、被切开的相邻块并成一块）；改说话人 {"kind": "relabel", "block": 块号, "speaker": 标签}。
apply_changes 的 kinds 限定这一步收哪几种类型，其余按 wrong_stage 拒绝，不展示给用户。

逐条按顺序应用到当前文本上，过不了的闸的那一条整条不应用，原因写进 rejected，其余照常。闸门只管"改得合不合规"（位置对得上、数字不乱动、
说话人是已有的、不丢字），不判断改得对不对：对不对由模型结合上下文和热词候选决定，有疑问的列给用户确认。
用户明确指定的改动（by="user"）只查位置：old 要找得到、唯一（或带 all），不改说话人标签。
"""
import json
import os
import re
from difflib import SequenceMatcher

import yaml
from pypinyin import Style, lazy_pinyin

DEFAULT_GATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gates.yaml")
_LABEL = re.compile(r"\A[A-Za-z0-9.]+：")  # 块首的说话人标签，不许改
# 阿拉伯数字（带万、亿），以及后面跟着单位的中文数字。"一直、一般、一个"里的"一"不是数字
_NUMBER = re.compile(r"[0-9][0-9.,]*[万亿]?|[零一二三四五六七八九十百千两]+(?:[万亿]|(?=[元块年月日号%倍]))")
_KINDS = ("fix", "tidy", "number_fix")
_STRUCTURE = ("split", "merge", "relabel")
KIND_LABELS = {"tidy": "整理", "fix": "纠错", "number_fix": "数字更正", "split": "拆块", "merge": "合并", "relabel": "改说话人"}
APPLICABLE = ("number", "rule")  # 被拦下但用户确认后可以应用的原因；其余是位置或格式有问题，应用不了
WRONG_STAGE = "wrong_stage"  # 这一步不收的类型（原始转录不订正错别字）：只回给模型，不列给用户
UNLISTED = (WRONG_STAGE,)  # 不列给用户的拒绝原因
_INITIALS = (("zh", "z"), ("ch", "c"), ("sh", "s"))
_FINALS = (("ang", "an"), ("eng", "en"), ("ing", "in"))


class GateError(ValueError):
    """输入本身不对（文件格式、阈值缺项）。单条改动不合规不抛这个，进 rejected。"""


def load_gates(path=None):
    try:
        with open(path or DEFAULT_GATES, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        raise GateError(f"读不到闸门配置：{e}") from e
    for key in ("max_delete_ratio", "min_keep", "max_grow"):
        v = raw.get(key) if isinstance(raw, dict) else None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise GateError(f"闸门配置 {key} 必须是正数，现为 {v!r}")
    return raw


def load_transcript(path):
    """读 export_transcript 导出的转写稿 json。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        blocks = data["blocks"]
        if not blocks or not all(isinstance(b.get("ord"), int) and isinstance(b.get("text"), str) for b in blocks):
            raise ValueError("blocks 为空或缺 ord、text")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise GateError(f"转写稿文件读不了或格式不对：{e}") from e
    return data


def _sounds(text):
    out = []
    for p in lazy_pinyin(text, style=Style.NORMAL, errors=lambda s: list(s)):
        for a, b in _INITIALS:
            if p.startswith(a):
                p = b + p[len(a):]
                break
        for a, b in _FINALS:
            if p.endswith(a):
                p = p[:-len(a)] + b
                break
        out.append(p)
    return out


def sound_similarity(a, b):
    """两段文字读音的相似度（忽略声调，翘舌平舌、前后鼻音视为同音），0 到 1。"""
    return SequenceMatcher(None, _sounds(a), _sounds(b)).ratio()


def _squash(text):
    return re.sub(r"\s+", "", text)  # 数字中间或后面的空格（"690 万"）不算数字变了


def numbers(text):
    """文字里的数字（带单位的写法算一个整体）。"""
    return _NUMBER.findall(text)


def _is_subsequence(new, old):
    """new 是不是 old 删去若干字后的结果。"""
    it = iter(old)
    return all(c in it for c in new)


def _duplicated_fragment(old, new):
    """old 是 new 里某一段连写了两遍（700700 对 700，202025 对 2025），语音识别常见的数字重复。"""
    for k in range(1, len(old) // 2 + 1):
        for i in range(len(old) - 2 * k + 1):
            if old[i:i + k] == old[i + k:i + 2 * k] and old[:i + k] + old[i + 2 * k:] == new:
                return True
    return False


_REPEATED = re.compile(r"(\d[\d.]*[^\W\d_]{0,2})[，、。,. ]+\1")
_PUNCT = re.compile(r"[，。、？！,.?!\s]")


def _bare(s):
    return _PUNCT.sub("", s)


def _number_fix_basis(old, new, elsewhere):
    """number_fix 的依据：只许数字变，且每处变化是片段重复，或改后的数字在全文别处原样出现过。返回依据，不合规返回 None。"""
    if _bare(_REPEATED.sub(r"\1", old)) == _bare(new) and _bare(old) != _bare(new):
        return "数字片段重复"  # “是22年，22年”→“是22年”：整段带单位的数字说了两遍
    before, after = _NUMBER.findall(old), _NUMBER.findall(new)
    if len(before) != len(after) or before == after:
        return None
    basis = set()
    for o, n in zip(before, after):
        if o == n:
            continue
        if _duplicated_fragment(o, n):
            basis.add("数字片段重复")
        elif n in elsewhere:
            basis.add("全文别处出现过改后的数字")
        else:
            return None
    return "；".join(sorted(basis))


def _position_problem(change, text, lab_end):
    """位置和格式问题，用户指定的改动也要过。返回 (原因码, 说明)，没问题返回 None。"""
    old, new, kind = change.get("old"), change.get("new"), change.get("kind", "fix")
    if kind not in _KINDS:
        return "invalid", f"kind 必须是 {' 或 '.join(_KINDS)}"
    if not isinstance(old, str) or not old or not isinstance(new, str):
        return "invalid", "old 必须是非空字符串，new 必须是字符串"
    if old == new:
        return "same", "改前改后相同"
    n = text.count(old)
    if n == 0:
        return "missing", "old 在该块当前文本里找不到（逐字复制原文，注意前面的改动可能已改过这一处）"
    if n > 1 and not (change.get("all") and (kind == "tidy" or change.get("by") == "user")):
        return "ambiguous", f"old 在该块里出现 {n} 次，无法确定改哪一处，请带上前后文让它唯一"
    if text.index(old) < lab_end:
        return "label", "不能改说话人标签"
    return None


def _verdict(change, text, lab_end, gates, deleted_so_far, original_len, elsewhere):
    """返回 (None, 依据) 表示通过，否则是 (原因码, 不通过的原因)。原因码见 APPLICABLE。"""
    problem = _position_problem(change, text, lab_end)
    if problem:
        return problem
    if change.get("by") == "user":
        return None, ""
    old, new, kind = change["old"], change["new"], change.get("kind", "fix")
    if change.get("moved"):
        return None, "数字只是挪到了相邻的块"
    if kind == "number_fix":
        if _NUMBER.findall(old) == _NUMBER.findall(new):
            return "rule", "number_fix 只用于改数字；没改数字的改动请用 fix 或 tidy"
        basis = _number_fix_basis(old, new, elsewhere)
        if basis is None:
            return "number", "数字改动不自动应用：只有数字片段重复（如 700700→700），或改后的数字在全文别处出现过，才算明显错误，其余请列给用户确认"
        return None, basis
    before, after = _NUMBER.findall(_squash(old)), _NUMBER.findall(_squash(new))
    if kind == "tidy" and new and not _is_subsequence(new, old):
        return "rule", "整理只能删字：改后必须是改前删去若干字的结果"
    if set(before) != set(after):
        return "number", "涉及数字的改动不自动应用：转写里的数字改错的代价大。明显的数字重复请用 kind=number_fix，其余请列给用户确认"
    if kind == "fix":
        if not new:
            return "rule", "纠错不能把内容删空；要删口语废话请用 kind=tidy"
    elif deleted_so_far + (len(old) - len(new)) * text.count(old) > original_len * gates["max_delete_ratio"]:
        return "rule", "这个块被删掉的内容已超过比例上限"
    return None, ""


def _split_problem(change, text, labels):
    """拆块只做机械检查，不判断该不该拆、拆几段：至少两段、说话人是转写里已有的标签、各段拼起来正好是块的原文（不丢字、不改字）。没问题返回 None。"""
    parts = change.get("parts")
    if not (isinstance(parts, (list, tuple)) and len(parts) >= 2 and all(
            isinstance(p, (list, tuple)) and len(p) == 2 and isinstance(p[0], str) and isinstance(p[1], str) and p[1].strip() for p in parts)):
        return "split 必须有 parts：至少两段 [说话人标签, 这一段原文]，每段都不能为空"
    unknown = sorted({p[0] for p in parts} - labels)
    if unknown:
        return f"说话人 {'、'.join(unknown)} 不是转写里的说话人，可选：{'、'.join(sorted(labels))}"
    lab = _LABEL.match(text)
    if "".join(p[1] for p in parts) != text[lab.end() if lab else 0:]:
        return "各段拼起来必须正好等于这个块当前的原文，不能增减改字（改字放到拆块之后的纠错里）"
    return None


def _body(text):
    lab = _LABEL.match(text)
    return text[lab.end():] if lab else text


def _merge_problem(change, blocks, order, labels):
    """合并只做机械检查：至少两块、都存在、在当前顺序里紧挨着、说话人相同。没问题返回 None。"""
    ords = change.get("blocks")
    if not (isinstance(ords, (list, tuple)) and len(ords) >= 2 and all(isinstance(o, int) and not isinstance(o, bool) for o in ords)):
        return "merge 必须有 blocks：至少两个块号"
    missing = [o for o in ords if o not in order]
    if missing:
        return f"块 {'、'.join(map(str, missing))} 不存在（或已被并入别的块）"
    at = order.index(ords[0])
    if order[at:at + len(ords)] != list(ords):
        return "合并的块必须紧挨着，且按顺序写"
    speakers = {(_LABEL.match(blocks[o]["text"]) or [""])[0] for o in ords}
    if len(speakers) != 1:
        return "说话人不同的块不能合并；先改说话人，或确认确实是同一个人在说"
    return None


def apply_changes(transcript, changes, gates=None, kinds=None):
    """返回 {"blocks": [{"ord", "text", "original"}], "applied": [...], "rejected": [{"change", "reason"}]}。"""
    gates = gates or load_gates()
    if not isinstance(changes, list):
        raise GateError("changes 必须是列表")
    blocks = {b["ord"]: {"ord": b["ord"], "text": b["text"], "original": b.get("original", b["text"]), "anchor": b.get("anchor")}
              for b in transcript["blocks"]}
    deleted = {o: 0 for o in blocks}
    labels = {m.group(0)[:-1] for b in blocks.values() if (m := _LABEL.match(b["original"]))}
    order = [b["ord"] for b in transcript["blocks"]]  # 当前的块顺序：拆出来的新块紧跟在原块后面，合并掉的块从这里去掉
    next_ord = max(blocks) + 1
    applied, rejected = [], []
    for index, ch in enumerate(changes):
        kind = ch.get("kind", "fix") if isinstance(ch, dict) else None
        key = ch.get("block") if isinstance(ch, dict) else None
        if kind == "merge" and isinstance(ch.get("blocks"), (list, tuple)) and ch["blocks"]:
            key = ch["blocks"][0]
        block = blocks.get(key)
        if block is None:
            rejected.append({"index": index, "change": ch, "code": "invalid", "reason": "block 不存在", "applicable": False})
            continue
        if kinds is not None and kind not in kinds:
            rejected.append({"index": index, "change": ch, "code": WRONG_STAGE, "applicable": False,
                             "reason": f"这一步只能提 {'、'.join(kinds)}；其他类型的改动放到下一步，这一步不要提"})
            continue
        if kind == "merge":
            why = _merge_problem(ch, blocks, order, labels)
            if why:
                rejected.append({"index": index, "change": ch, "code": "invalid", "reason": why, "applicable": False})
                continue
            rest = [blocks[o] for o in ch["blocks"][1:]]
            shown = " ｜ ".join(_body(blocks[o]["text"]) for o in ch["blocks"])
            block["text"] += "".join(_body(b["text"]) for b in rest)
            block["original"] += "".join(_body(b["original"]) for b in rest)
            try:  # 合并后的时间从第一块的开头到最后一块的结尾
                block["anchor"] = {**block["anchor"], "time_ms": [block["anchor"]["time_ms"][0], rest[-1]["anchor"]["time_ms"][1]]}
            except (KeyError, TypeError, IndexError):
                pass
            deleted[block["ord"]] += sum(deleted.pop(b["ord"]) for b in rest)
            for b in rest:
                order.remove(b["ord"])
            applied.append({**ch, "block": block["ord"], "index": index, "old": shown, "new": _body(block["text"])})
            continue
        if kind == "relabel":
            speaker, lab = ch.get("speaker"), _LABEL.match(block["text"])
            why = ("speaker 必须是转写里已有的说话人：" + "、".join(sorted(labels))) if speaker not in labels else (
                "这个块没有说话人标签" if not lab else ("改前改后相同" if lab.group(0)[:-1] == speaker else None))
            if why:
                rejected.append({"index": index, "change": ch, "code": "invalid", "reason": why, "applicable": False})
                continue
            was = lab.group(0)[:-1]
            block["text"] = f"{speaker}：{block['text'][lab.end():]}"
            applied.append({**ch, "index": index, "old": was, "new": speaker})
            continue
        if kind == "split":
            why = _split_problem(ch, block["text"], labels)
            if why:
                rejected.append({"index": index, "change": ch, "code": "invalid", "reason": why, "applicable": False})
                continue
            (first_speaker, first), rest = ch["parts"][0], ch["parts"][1:]
            block["text"] = f"{first_speaker}：{first}"
            extra = []
            for speaker, part in rest:
                text = f"{speaker}：{part}"
                blocks[next_ord] = {"ord": next_ord, "text": text, "original": text, "anchor": block["anchor"]}
                deleted[next_ord] = 0
                extra.append(next_ord)
                next_ord += 1
            at = order.index(block["ord"]) + 1
            order[at:at] = extra
            applied.append({**ch, "index": index, "new_ords": extra})
            continue
        lab = _LABEL.match(block["text"])
        elsewhere = set()
        if ch.get("kind") == "number_fix" and isinstance(ch.get("old"), str):
            elsewhere = set(_NUMBER.findall(" ".join(blocks[o]["text"] if blocks[o] is not block else block["text"].replace(ch["old"], "", 1)
                                                      for o in order)))
        code, why = _verdict(ch, block["text"], lab.end() if lab else 0, gates, deleted[block["ord"]], len(block["original"]), elsewhere)
        if code:
            rejected.append({"index": index, "change": ch, "code": code, "reason": why, "applicable": code in APPLICABLE})
            continue
        n = block["text"].count(ch["old"]) if ch.get("all") else 1
        block["text"] = block["text"].replace(ch["old"], ch["new"], n)
        if ch.get("kind", "fix") == "tidy":
            deleted[block["ord"]] += (len(ch["old"]) - len(ch["new"])) * n
        applied.append({**ch, "index": index, **({"basis": why} if why else {})})
    return {"source": transcript.get("source", ""), "blocks": [blocks[o] for o in order], "applied": applied, "rejected": rejected}

def save(result, stem):
    """写三个文件：{stem}_校正稿.md（干净的校正稿）、{stem}_修改清单.md（逐处对照，供核对）、{stem}_校正稿.json（后续还要改时用）。
    返回文件名列表。"""
    names = [f"{stem}_校正稿.md", f"{stem}_修改清单.md", f"{stem}_校正稿.json"]
    with open(names[0], "w", encoding="utf-8") as f:
        f.write("\n\n".join(b["text"] for b in result["blocks"]) + "\n")
    shown = [r for r in result["rejected"] if r["code"] not in UNLISTED]
    lines = [f"# {stem} 修改清单", "", f"共 {len(result['applied'])} 处修改，{len(shown)} 处未应用。", ""]
    if result["applied"]:
        lines += ["## 已修改", "", "| 块 | 类型 | 原文 | 改后 | 依据 |", "|---|---|---|---|---|"]
        lines += [f"| b{c['block']} | {KIND_LABELS.get(c.get('kind', 'fix'), '纠错')} | {c.get('old', '')} | "
                  f"{('拆成 ' + str(len(c['parts'])) + ' 段') if c.get('kind') == 'split' else (c['new'] or '（删除）')} | {c.get('reason', '')} |"
                  for c in result["applied"]]
    if shown:
        lines += ["", "## 未应用", "", "| 块 | 原文 | 想改成 | 原因 |", "|---|---|---|---|"]
        lines += [f"| b{r['change'].get('block')} | {r['change'].get('old')} | {r['change'].get('new')} | {r['reason']} |" for r in shown]
    with open(names[1], "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(names[2], "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    return names
