"""给校订稿的正文标色。标色文字必须原样出现在正文里，所以只加标记，不可能改动正文。
模型给的实体对全文每个块生效（出现几处标几处）；金额、比例、数量、日期由代码按规则标。"""
import re

from eva_report.inline import MARKS

_AUTO = (("时间", re.compile(r"\d{2,4}年(?:\d{1,2}月)?(?:\d{1,2}[日号])?|\d{1,2}月(?:份|\d{1,2}[日号])?")),
         ("数字", re.compile(r"\d[\d.,]*(?:\s?[~～到至]\s?\d[\d.,]*)?(?:[万亿]元?|元|块钱|块|%|倍|个|笔)|\d[\d.,]*\s?[~～]\s?\d[\d.,]*")))  # 区间整段标，"1400~1500吧"没单位也是数字
_ORG = re.compile(r"[一-鿿]{0,3}?(有限公司|股份|集团|银行|农商行|供应链|分行|支行|公司)")
_PRONOUN_WORDS = ("我们", "咱们", "你们", "他们", "贵", "本", "该", "这个", "那个", "整个", "一家", "两家")
_FUNCTION_CHARS = "的个家是在跟和与给到下后上前把对从向往还又也都就才都先再没有"


def check(entities, bodies, skipped):
    """整理模型给的实体。格式不对、类型不认识、全文里找不到的跳过并记原因，不因此拦下整份记录。返回可用的 [(类型, 文字)]。"""
    usable = []
    for item in entities or []:
        if not (isinstance(item, (list, tuple)) and len(item) == 2 and all(isinstance(x, str) for x in item)):
            skipped.append(f"标色项必须是 (类型, 文字)：{item!r}")
            continue
        kind, sub = item
        if kind not in MARKS:
            skipped.append(f"标色类型 {kind!r} 不认识（{'、'.join(MARKS)}）：{sub!r}")
        elif not sub.strip() or any(c in sub for c in "[]|\n"):
            skipped.append(f"标色文字 {sub!r} 不能为空，也不能含 [ ] | 或换行")
        elif not any(sub in b for b in bodies):
            skipped.append(f"校订稿里没有 {sub!r}，标色文字要和校订稿一字不差")
        else:
            usable.append((kind, sub))
    return usable


def spans_of(body, entities):
    """[(起, 止, 类型)]，按位置排序。先占位的是模型给的实体（长的优先），再是自动识别的，重叠的不重复标。"""
    spans = []

    def add(start, end, kind):
        if not any(start < e and s < end for s, e, _ in spans):
            spans.append((start, end, kind))

    for kind, sub in sorted(entities, key=lambda x: -len(x[1])):
        at = body.find(sub)
        while at != -1:
            add(at, at + len(sub), kind)
            at = body.find(sub, at + len(sub))
    for kind, pattern in _AUTO:
        for m in pattern.finditer(body):
            add(m.start(), m.end(), kind)
    return sorted(spans)


def render(body, spans, inserts=()):
    """把标色写成 [[类型|文字]]。inserts 是 [(位置, 文字)]，在该位置插入文字；落在某处标色中间的挪到那处标色后面。"""
    moved = []
    for p, t in inserts:
        inside = next((e for s, e, _ in spans if s < p < e), None)
        moved.append((p if inside is None else inside, t))
    ins, out, pos, i = sorted(moved, key=lambda x: x[0]), [], 0, 0

    def flush(upto):
        nonlocal pos, i
        while i < len(ins) and ins[i][0] <= upto:
            out.extend((body[pos:ins[i][0]], ins[i][1]))
            pos, i = ins[i][0], i + 1
        out.append(body[pos:upto])
        pos = upto

    for s, e, kind in spans:
        flush(s)
        out.append(f"[[{kind}|{body[s:e]}]]")
        pos = e
    flush(len(body))
    return "".join(out)


def _name_of(match):
    """"咱们公司""后建设银行"这类，去掉前面的人称和虚词，剩下的名字部分。名字不足 2 个字就不是公司名。"""
    prefix = match.group(0)[:match.start(1) - match.start(0)]
    for p in _PRONOUN_WORDS:
        if p in prefix:
            prefix = prefix.rsplit(p, 1)[1]
    prefix = prefix.lstrip(_FUNCTION_CHARS)
    return prefix + match.group(1) if len(prefix) >= 2 else None


def hints_of(body, spans, ord_, reported):
    """正文里像公司名、却没被标色盖住的词。口头的"我们公司"这类不算。reported 记已提示过的词，同一个词只提示一次。"""
    out = []
    for m in _ORG.finditer(body):
        word = _name_of(m)
        if word is None or word in reported or any(s < m.end() and m.start() < e for s, e, _ in spans):
            continue
        reported.add(word)
        out.append(f"块 {ord_} 的「{word}」附近像公司名，没标色；是的话加进 entities 重新生成")
    return out
