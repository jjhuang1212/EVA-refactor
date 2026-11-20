"""校订：模型只交"改好以后的块文本"（可以带说话人标签拆成几段、改说话人；相邻同一人的块先自动并成一块），由这里和当前文本逐字对比，
算出改动清单再交给 apply_changes。模型不用逐字复述原文来定位，也不受"一次只能改几个字"的限制：整句改顺、乱句修好都行。

保证不丢信息的检查有两层：这里按块检查（改后字数不能缩成摘要或膨胀、和改前对得上的字不能太少），不过的整块不改，原因回给模型；
apply_changes 逐处检查（数字不能乱动、说话人必须是已有的）。
"""
import re
from collections import Counter
from difflib import SequenceMatcher

from eva_asr.diff_gate import GateError, _body, _squash, _is_subsequence, load_gates, numbers

_LABEL = re.compile(r"\A([A-Za-z0-9.]+)：")
_TAIL, _HEAD = re.compile(r"[0-9.,]*\Z"), re.compile(r"[0-9.,]*")
_SHORT = 10  # 短于这个字数的块不查字数比例，一两个字的改动就会超


def _parts(ord_, value, label):
    """校订值 -> [(说话人或 None, 文字)]。字符串一行就是一段（可带标签，标签和块原来的不同就是改说话人）；多行或列表是几段，每段要有标签。"""
    if isinstance(value, str):
        lines = [x for x in value.split("\n") if x.strip()]
        if not lines:
            raise GateError(f"块 {ord_}：校订值不能是空的")
        if len(lines) == 1:
            m = _LABEL.match(lines[0])
            return [(m.group(1) if m else None, lines[0][m.end():] if m else lines[0])]
        value = []
        for line in lines:
            m = _LABEL.match(line)
            if not m:
                raise GateError(f"块 {ord_}：拆成几行时每行都要以 标签： 开头，这一行没有：{line[:20]}")
            value.append((m.group(1), line[m.end():]))
    if not (isinstance(value, (list, tuple)) and value and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in value)):
        raise GateError(f"块 {ord_}：校订值要么是一段文字，要么是几行 标签：文字，要么是 [(标签, 文字), ...]")
    return [(p[0], p[1]) for p in value]


def _groups(old, new):
    """old 到 new 的差异：[(i1, i2, j1, j2)]。不把隔着不变字的两处并起来：并了，旁边的整句改写会拖着一处正确的同音纠错一起被拒。"""
    return [o[1:] for o in SequenceMatcher(None, old, new, autojunk=False).get_opcodes() if o[0] != "equal"]


def hunks(block, old, new, track=False):
    """一段文字里的改动，按从左到右的顺序，每条的 old 在应用到它那一步的文本里唯一。track 给了就带上每处删掉、加上的数字（_cut、_put），供 _mark_moved 用。"""
    out, work, shift = [], old, 0
    for i1, i2, j1, j2 in _groups(old, new):
        s, e = i1 + shift, i2 + shift
        cut, put = work[s:e], new[j1:j2]
        lead, tail = _TAIL.search(work[:s]).group(0), _HEAD.match(work[e:]).group(0)  # 数字别从中间切开，闸门要看整个数
        s, e, cut, put = s - len(lead), e + len(tail), lead + cut + tail, lead + put + tail
        c = 1
        while True:
            lo, hi = max(s - c, 0), min(e + c, len(work))
            if work[lo:hi] and work.count(work[lo:hi]) == 1:
                break
            if lo == 0 and hi == len(work):
                raise GateError(f"块 {block}：改动 {cut!r}→{put!r} 在块里找不到唯一的位置")
            c += 1
        before = work[lo:hi]
        after = work[lo:s] + put + work[e:hi]
        kind = "number_fix" if set(numbers(_squash(cut))) != set(numbers(_squash(put))) else ("tidy" if _is_subsequence(put, cut) else "fix")
        out.append({"block": block, "old": before, "new": after, "kind": kind, "reason": "校订", **({"_cut": numbers(_squash(cut)), "_put": numbers(_squash(put))} if track else {})})
        work, shift = work[:s] + put + work[e:], shift + len(put) - len(cut)
    return out


def _mark_moved(found, order):
    """一句话被挪到相邻的块（转录常在句子中间断块），数字在一块里少了、在另一块里多了，合起来没变。两处都标 moved，闸门不把它当改数字。"""
    pos = {o: i for i, o in enumerate(order)}
    gone = [(c, Counter(c["_cut"]) - Counter(c["_put"])) for c in found]
    for a, lost in gone:
        if not lost or a.get("moved"):
            continue
        for b, _ in gone:
            if not b.get("moved") and abs(pos[a["block"]] - pos[b["block"]]) == 1 and Counter(b["_put"]) - Counter(b["_cut"]) == lost:
                a["moved"] = b["moved"] = True
                a["kind"] = b["kind"] = "fix"
                break
    for c in found:
        del c["_cut"], c["_put"]


def _boundary(old, new, p):
    """new 里的第 p 个字符之前，对应 old 里的哪个位置。落在改动区域里时取该区域的起点。"""
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal" and j1 <= p <= j2:
            return i1 + (p - j1)
        if j1 <= p < j2 or (p == j1 and tag != "equal"):
            return i1
    return len(old)


def _problem(old, new, gates):
    """一个块改前改后的整体检查：是不是缩成了摘要、补写了内容、整段换成了无关的话。允许改写。没问题返回 None。"""
    if not new.strip():
        return "改后是空的"
    if len(old) < _SHORT:
        return None
    ratio = len(new) / len(old)
    if not gates["min_keep"] <= ratio <= gates["max_grow"]:
        return f"改后的字数是改前的 {ratio:.0%}，应在 {gates['min_keep']:.0%} 到 {gates['max_grow']:.0%} 之间：可以改写得简练，但不能缩成摘要，也不能补写没说过的内容"
    return None


def same_speaker_runs(blocks):
    """相邻且说话人相同的块号分组 [[3, 4, 5], ...]（只含两块以上的）。转录模型常把一个人的一段话按停顿切成几块，一个人连着说的话本来就是一段。"""
    runs, cur, last = [], [], None
    for b in blocks:
        m = _LABEL.match(b["text"])
        label = m.group(1) if m else object()
        if cur and label == last:
            cur.append(b["ord"])
        else:
            if len(cur) > 1:
                runs.append(cur)
            cur = [b["ord"]]
        last = label
    if len(cur) > 1:
        runs.append(cur)
    return runs


def proofread_changes(blocks, corrected, normalize=None, gates=None):
    """blocks 是当前的块（apply_changes 返回里的 blocks），corrected 是 {块号: 校订值}。返回 (改动清单, 被还原的块)：
    改动清单交给 apply_changes，被还原的是 [{"block", "reason"}]，只给模型看。

    校订值按原始块号写，不预先合并相邻同一人的块（短促的应答并进长句里，模型会当噪音删掉，也分不开说话人）。按块号从小到大处理：拆块和改说话人
    （新块的块号是依次往后排的，后面各段里的纠错按这个预测的块号写），然后是逐处纠错；最后把相邻同一人的块并成一块。
    没提交的块视为不改。块号不存在、文字对不上标签等输入本身的错误抛 GateError。
    normalize 给了就先用它处理每一段提交的文字：模型常从原始转录里抄文字，把已经清掉的语气词、口吃又抄了回来，先按同样的规则清一遍。
    """
    gates = gates or load_gates()
    by_ord = {b["ord"]: dict(b) for b in blocks}
    order = [b["ord"] for b in blocks]
    labels = {m.group(1) for b in blocks if (m := _LABEL.match(b["text"]))}
    next_ord = max(by_ord) + 1
    structure, found, refused, late = [], [], [], {}
    seq = [[o, (_LABEL.match(by_ord[o]["text"]) or [None, None])[1]] for o in order]  # 每块现在的说话人，最后一遍合并用
    for ord_ in sorted(corrected):
        if ord_ not in order:
            raise GateError(f"块 {ord_} 不存在（或已并入别的块），现有的块号是 {min(by_ord)} 到 {max(by_ord)}")
        label = _LABEL.match(by_ord[ord_]["text"])
        old = by_ord[ord_]["text"][label.end():] if label else by_ord[ord_]["text"]
        parts = _parts(ord_, corrected[ord_], label.group(1) if label else None)
        unknown = sorted({s for s, _ in parts if s} - labels)
        if unknown:
            raise GateError(f"块 {ord_}：说话人 {'、'.join(unknown)} 不是转写里的说话人，可选：{'、'.join(sorted(labels))}")
        if normalize:
            parts = [(spk, normalize(text)) for spk, text in parts]
        why = _problem(old, "".join(t for _, t in parts), gates)
        if why and (len(parts) > 1 or not parts[0][1].strip()):
            refused.append({"block": ord_, "reason": why})
            continue
        if why:
            late[ord_] = why  # 字数超限，可能只是别处的句子挪进来了；要等挪动配对后才能定
        at = next(i for i, s in enumerate(seq) if s[0] == ord_)
        if len(parts) == 1:
            speaker, new = parts[0]
            if label and speaker and speaker != label.group(1):
                structure.append({"kind": "relabel", "block": ord_, "speaker": speaker, "reason": "说话人归错了"})
                seq[at][1] = speaker
            found += hunks(ord_, old, new, True)
            continue
        full = "".join(t for _, t in parts)
        cuts, pos = [0], 0
        for _, text in parts[:-1]:
            pos += len(text)
            cuts.append(_boundary(old, full, pos))
        cuts.append(len(old))
        pieces = [old[a:b] for a, b in zip(cuts, cuts[1:])]
        if not all(p.strip() for p in pieces):
            refused.append({"block": ord_, "reason": "拆出来的某一段在原文里找不到对应的字，请把每一段写完整"})
            continue
        structure.append({"kind": "split", "block": ord_, "parts": [[spk or (label.group(1) if label else ""), piece] for (spk, _), piece in zip(parts, pieces)],
                          "reason": "混进了几个人的话"})
        ords = [ord_] + list(range(next_ord, next_ord + len(parts) - 1))
        next_ord += len(parts) - 1
        seq[at:at + 1] = [[o, spk or (label.group(1) if label else None)] for o, (spk, _) in zip(ords, parts)]
        for block, piece, (_, text) in zip(ords, pieces, parts):
            found += hunks(block, piece, text, True)
    _mark_moved(found, [o for o, _ in seq])
    for o, why in late.items():
        if any(c.get("moved") and c["block"] == o for c in found):
            continue  # 句子挪动造成的增减不按字数比例查
        refused.append({"block": o, "reason": why})
        found = [c for c in found if c["block"] != o]
        structure = [c for c in structure if not (c["kind"] == "relabel" and c["block"] == o)]
    again = same_speaker_runs([{"ord": o, "text": f"{s}：" if s else ""} for o, s in seq])
    return structure + found + [{"kind": "merge", "blocks": g, "reason": "同一人连着说的话"} for g in again], refused