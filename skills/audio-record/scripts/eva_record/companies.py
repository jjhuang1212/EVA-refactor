"""涉及公司表、需要用户确认的事项，以及发给用户的说明文字。这些都由代码根据结果生成，不靠模型每次现编。"""
import re
from collections import Counter
from difflib import SequenceMatcher

from eva_asr import sound_similarity
from eva_asr.diff_gate import numbers

from eva_record import changes as ch
from eva_record.errors import RecordError

CONFIRMED, PENDING = "已确认", "待确认"
LIMIT = 10  # 请用户确认的事项最多几条，再多用户不会看
_SAME_SOUND = 0.8
_HOMOPHONE = 0.99
_SHORT = 20
_IDENTIFIER = re.compile(r"[A-Za-z]+_[A-Za-z_]+")  # resolve_entity、web_search 这类工具名


def check(rows):
    """涉及公司表的格式：每行 {"spoken": [口述写法...], "name": 全称, "basis": 依据, "status": 已确认|待确认}，可另带 legal（法人）、uscc（统一社会信用代码）。格式不对直接报错。
    状态由主 agent 按 SKILL 的门槛判断，这里不替它改；用户在 PDF 的“涉及公司”表里能看到依据。"""
    out = []
    for r in rows or []:
        ok = (isinstance(r, dict) and isinstance(r.get("spoken"), list) and r["spoken"] and all(isinstance(x, str) and x.strip() for x in r["spoken"])
              and all(isinstance(r.get(k), str) and r[k].strip() for k in ("name", "basis")) and r.get("status") in (CONFIRMED, PENDING))
        if not ok:
            raise RecordError(f"涉及公司的每一行要有 spoken（口述写法列表）、name（全称）、basis（依据）、status（{CONFIRMED} 或 {PENDING}）：{r!r}")
        if _IDENTIFIER.search(r["basis"]):
            raise RecordError(f"涉及公司的 basis 是写给用户看的，不要出现工具名或字段名（{_IDENTIFIER.search(r['basis']).group(0)}），改成一句人话：{r!r}")
        if r["name"].strip() in [x.strip() for x in r["spoken"]] and r["status"] == CONFIRMED:
            continue  # 已确认的却和口述写法一样，表里没有新信息；没查到全称的（待确认）要留着，问用户
        extra = {k: r[k] for k in ("legal", "uscc") if k in r}
        if not all(isinstance(v, str) and v.strip() for v in extra.values()):
            raise RecordError(f"涉及公司的 legal（法人）、uscc（统一社会信用代码）要么不写，要么是非空字符串：{r!r}")
        extra = {k: v.strip() for k, v in extra.items()}
        out.append({"spoken": [x.strip() for x in r["spoken"]], "name": r["name"].strip(), "basis": r["basis"].strip(), "status": r["status"], **extra})
    return out


def _short(text):
    text = (text or "").replace("\n", " ")
    return text if len(text) <= _SHORT else text[:_SHORT] + "…"


def _variants(entities, bodies):
    """读音几乎一样、写法不同的公司名（聚石环境/巨石环境）。互相包含的（广东聚石运营/广东聚石运营管理有限公司）是同一写法的长短，不算。"""
    text = "\n".join(bodies)
    names = sorted({e[1] for e in entities if isinstance(e, (list, tuple)) and len(e) == 2 and e[0] == "公司" and isinstance(e[1], str) and len(e[1]) >= 3 and e[1] in text})  # 正文里没出现的写法不用提
    groups = []
    for n in names:
        for g in groups:
            if any(n not in m and m not in n and sound_similarity(n, m) >= _SAME_SOUND for m in g):
                g.append(n)
                break
        else:
            groups.append([n])
    return [[(n, text.count(n)) for n in g] for g in groups if len(g) > 1]


def _homophone_edits(changes, entities, companies):
    """模型把公司名里的字改成了读音一样的另一个字（巨石→聚石）：{改后的名字: 改前的名字}。没有已确认的依据，这种改动要让用户知道。"""
    names = [e[1] for e in entities if isinstance(e, (list, tuple)) and len(e) == 2 and e[0] == "公司" and isinstance(e[1], str)]
    settled = {s for r in companies if r["status"] == CONFIRMED for s in r["spoken"]} | {r["name"] for r in companies if r["status"] == CONFIRMED}
    out = {}
    for c in changes:
        if c["status"] != ch.APPLIED or c["kind"] != "fix" or not c.get("old") or not c.get("new"):
            continue
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, c["old"], c["new"], autojunk=False).get_opcodes():
            a, b = c["old"][i1:i2], c["new"][j1:j2]
            if tag != "replace" or sound_similarity(a, b) < _HOMOPHONE:
                continue
            for n in names:
                before = n.replace(b, a, 1)
                if b in n and n not in settled and before not in settled:
                    out[n] = before
    return out


def _only_drops_a_repeated_number(c, bodies):
    """想删掉的数字在稿子里还有别处，而且这处改动没引入新数字：不删只是啰嗦，没有信息损失，不拿这种事问用户（修改清单里仍有这条）。"""
    before, after = numbers(c["old"] or ""), numbers(c["new"] or "")
    gone = Counter(before) - Counter(after)
    text = " ".join(bodies)
    return bool(gone) and set(after) <= set(before) and all(text.count(n) >= 2 for n in gone)


def confirmations(changes, companies, entities, bodies):
    """需要用户确认的事项（文字）：写法不一致的公司名、被拦下待用户确认的改动、涉及公司表里待确认的行。最多 LIMIT 条。"""
    out, groups, edits = [], _variants(entities, bodies), _homophone_edits(changes, entities, companies)
    asked = {n for g in groups for n, _ in g} | set(edits) | set(edits.values())  # 这些写法上面已经问过了，下面的公司表不再重复问
    for g in groups:
        out.append("、".join(f"「{n}」（{c}处）" for n, c in g) + " 读音一样，像是同一家公司，请告诉我正确写法")
    for after, before in edits.items():
        out.append(f"「{before}」我写成了「{after}」（读音一样，没有可靠依据），对吗")
    for row in companies:
        if row["status"] == PENDING and not asked.intersection(row["spoken"]):
            if row["name"] in row["spoken"]:
                out.append(f"「{row['name']}」我没能确定全称（{row['basis']}），知道的话请告诉我")
            else:
                out.append(f"「{row['spoken'][0]}」我识别为「{row['name']}」（{row['basis']}），对吗")
    for c in changes:
        if c["status"] == ch.PENDING and c["kind"] != "split" and not _only_drops_a_repeated_number(c, bodies):
            found = ch.sentences(c["old"], c["new"], bodies) if c["old"] else None
            out.append(f"原句「{found[0]}」，我想改成「{found[1]}」，要改吗" if found else f"「{_short(c['old'])}」可能应为「{_short(c['new'])}」，我没有自动改，要改吗")
    return out[:LIMIT]


def message(changes, asks, failed=(), revised=False, listed=True):
    """发给用户的说明：做了什么、哪几处请确认、接下来怎么改。模型照这段转述，不用自己组织。"""
    applied = [c for c in changes if c["status"] == ch.APPLIED]
    n = lambda kind: sum(1 for c in applied if c["kind"] == kind)  # noqa: E731
    done = [t.format(k) for t, k in (("清掉了 {} 处语气词、乱码和重复", n("tidy")), ("纠正了 {} 处识别错误", n("fix")),
                                     ("拆开了 {} 处混在一起的发言", n("split")),
                                     ("合并了 {} 处被切断的发言", n("merge")), ("改正了 {} 处说话人", n("relabel")), ("更正了 {} 处数字（文末有说明）", n("number_fix"))) if k]
    lines = ["已按你说的改好，新的记录见 PDF。" if revised else "整理好了，最终记录见 PDF。"]
    if done and not revised:
        lines.append("这次" + "，".join(done) + "。")
    if failed:
        lines.append("这几处没能改：" + "；".join(failed))
    if asks:
        lines += ["有 %d 处想请你确认：" % len(asks)] + [f"{i}. {a}" for i, a in enumerate(asks, 1)]
        lines.append("直接回复就行，例如“聚石环境和巨石环境是同一家，写聚石环境”“把X改成Y”。")
    else:
        lines.append("没有需要你确认的地方。要改什么直接告诉我，例如“把X改成Y”“X是Y公司”。")
    if not listed and not revised:
        lines.append("想看具体改了哪些地方，回复“要修改清单”，我发给你。")
    return "\n".join(lines)

