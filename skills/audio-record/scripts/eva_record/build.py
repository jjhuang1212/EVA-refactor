"""把两轮闸门处理后的转写块拼成交付物（原始转录 md、按场景定制封面的记录 PDF、可选的修改清单）。

输入的块来自 eva_asr.apply_changes：stage1 是整理后的原始转录（去语气词、订正明显错别字），
stage2 是在 stage1 上按上下文纠错后的校订稿。块里的 text 以"S1："这样的说话人标签开头（没做说话人分离时没有），
anchor.time_ms 是这段话在录音里的起止毫秒。
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone

import yaml
from eva_asr import numbers

from eva_record import changes as ch
from eva_record import companies as co
from eva_record import paint
from eva_record.errors import RecordError

CST = timezone(timedelta(hours=8))
DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
_LABEL = re.compile(r"\A([A-Za-z0-9.]+)：")
_NUMBERED = re.compile(r"S(\d+)")
STAGE1_KINDS = ("tidy", "number_fix")  # 原始转录只整理不订正，纠错都在第二轮
LEGEND ="颜色标注：[[公司|公司名]] [[地址|地址]] [[人名|人名]] [[时间|时间]] [[数字|数字]]。标色处是关键信息，请优先核对。"


def load_config(path=None):
    try:
        with open(path or DEFAULT_CONFIG, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        raise RecordError(f"读不到录音记录配置：{e}") from e
    if not isinstance(cfg, dict) or not isinstance(cfg.get("emit_change_list"), bool):
        raise RecordError("录音记录配置 emit_change_list 必须是 true 或 false")
    scenarios = cfg.get("scenarios")
    if not isinstance(scenarios, dict) or not scenarios:
        raise RecordError("录音记录配置 scenarios 必须是非空映射")
    for name, sc in scenarios.items():
        ok = (isinstance(sc, dict) and all(isinstance(sc.get(k), str) and sc[k].strip() for k in ("record", "eyebrow", "notice"))
              and isinstance(sc.get("fields"), list) and all(isinstance(f, str) and f.strip() for f in sc["fields"]))
        if not ok:
            raise RecordError(f"场景 {name} 必须有 record、eyebrow、notice（非空字符串）和 fields（字符串列表）")
        people = sc.get("people", [])
        if not isinstance(people, list) or not set(people) <= set(sc["fields"]):
            raise RecordError(f"场景 {name} 的 people 必须是 fields 的子集（字符串列表）")
    return cfg


def stamp(ms):
    s = ms // 1000
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def _split(text):
    m = _LABEL.match(text)
    return (m.group(1), text[m.end():]) if m else ("", text)


def _name(label, speakers):
    """用户给了对应关系就用，没给的 S1 写成"说话人1"。"""
    if not label:
        return ""
    if speakers.get(label):
        return speakers[label]
    n = _NUMBERED.fullmatch(label)
    return f"说话人{n.group(1)}" if n else label


def _span(block):
    try:
        begin, end = block["anchor"]["time_ms"][:2]
        if any(isinstance(ms, bool) or not isinstance(ms, int) or ms < 0 for ms in (begin, end)):
            raise ValueError
    except (KeyError, TypeError, IndexError, ValueError):
        raise RecordError(f"块 {block.get('ord')} 没有可用的时间锚点，无法标时间戳") from None
    return f"{stamp(begin)}-{stamp(end)}"


def _lines(blocks, speakers):
    for b in blocks:
        label, body = _split(b["text"])
        if body.strip():
            yield b, _name(label, speakers), body.strip()


def raw_markdown(blocks, source_stem, speakers=None):
    """原始转录：每行"[b块号 起-止] 说话人：文字"，块号和修改清单、引用里的块号一致。"""
    speakers = speakers or {}
    out = [f"{source_stem} 原始转录", ""]
    for b, who, body in _lines(blocks, speakers):
        head = f"[b{b['ord']} {_span(b)}]"
        out.append(f"{head} {who}：{body}" if who else f"{head} {body}")
    if len(out) == 2:
        raise RecordError("整理后没有任何文字，不出原始转录")
    return "\n\n".join(out) + "\n"


def _scenario(cfg, name, info):
    """返回（场景配置, 封面信息项）。场景不认识就报错，不猜。用户给了什么项都写进封面：
    场景 fields 里的项排在前面并按配置顺序，其余的按给出的顺序跟在后面。"""
    sc = cfg["scenarios"].get(name)
    if sc is None:
        raise RecordError(f"没有场景 {name!r}，可选：{'、'.join(cfg['scenarios'])}")
    info = {str(k).strip(): _text(v) for k, v in (info or {}).items()}
    info = {k: v for k, v in info.items() if k and v}  # 内容为空就是没给，跳过
    return sc, [(f, info[f]) for f in sc["fields"] if f in info] + [(k, v) for k, v in info.items() if k not in sc["fields"]]


def _text(v):
    """信息项的内容转成一行文字：列表用顿号拼，其余转字符串。"""
    if isinstance(v, (list, tuple)):
        return "、".join(t for t in map(_text, v) if t)
    return "" if v is None else str(v).strip()


def _notes(blocks, origin, changes, source_name, skipped):
    """数字更正的角标。已应用且数字集合变了的改动（口吃重复里删掉多余的数字、整句挪到相邻块的数字不算）：正文里该数字后面加一条指向原始转写块的引用，
    文末“数字变更说明”一节按引用逐条写“时间 · 块 · 原数字→新数字”。返回 ({块号: [(位置, 引用文字)]}, {引用键: 说明})。
    blocks 是校订后的块（相邻同一人已合并，块号沿用第一块），origin 是合并前的原始块：引用要落在数字真正所在的原始块上，否则引用核对找不到原文。"""
    by_ord = {b["ord"]: b for b in blocks}
    ords = sorted(by_ord)
    inserts, notes = {}, {}
    for c in changes:
        if c["status"] != ch.APPLIED or c["kind"] in ("split", "merge", "relabel") or c.get("moved") or set(numbers(c["old"])) == set(numbers(c["new"])):
            continue
        # 自动合并后，原块号可能已并进别的块：按文字找现在所在的块
        b = by_ord.get(c["block"]) or next((x for x in blocks if c["old"] in x["original"] and c["new"] in x["text"]), None)
        if b is None:
            skipped.append(f"数字更正 {c['id']}（块 {c['block']}）所在的块已合并，没能找到位置，数字变更说明里没有这一项")
            continue
        c = {**c, "block": b["ord"]}
        orig, final = b["original"], b["text"]
        gone = [n for n in dict.fromkeys(numbers(c["old"])) if n not in numbers(c["new"])]
        came = [n for n in dict.fromkeys(numbers(c["new"])) if n not in numbers(c["old"])]
        pos = final.find(c["new"]) if c["new"] else -1
        if orig.find(c["old"]) < 0 or pos < 0 or not source_name or any(x in source_name for x in "#【】"):
            skipped.append(f"数字更正 {c['id']}（块 {c['block']}）没能在正文里加角标，数字变更说明里没有这一项")
            continue
        quote = gone[0] if gone else c["old"]  # 摘抄只取要改的数字片段，来源一节里不展示
        end = next((o for o in ords if o > b["ord"]), float("inf"))
        # 先找改动前后原文都对得上的块，再退到只含数字的块；落在自己合并范围内的优先（拆块后新块的块号不在原始转写里，只能全局找）
        hits = [x for key in (c["old"], quote) for x in origin if key in x["original"]]
        src = next((x for x in hits if b["ord"] <= x["ord"] < end), hits[0] if hits else None)
        if src is None:
            skipped.append(f"数字更正 {c['id']}（块 {c['block']}）在原始转写里没找到原数字所在的块，数字变更说明里没有这一项")
            continue
        inserts.setdefault(c["block"], []).append((pos + len(c["new"]), f"【{source_name}#b{src['ord']}「{quote}」】"))
        notes[("b", source_name.strip(), src["ord"], src["ord"], quote)] = f"{_span(src)} · 块{src['ord']} · {'、'.join(gone) or '无'}→{'、'.join(came) or ch.DELETED}"
    return inserts, notes


def _record(blocks, origin, entities, speakers, sc, meta, now, skipped, hints, changes, source_name, materials, companies):
    from eva_report import Report
    report = Report(sc["record"])
    people = [(k, v) for k, v in meta if k in sc.get("people", ())]
    tags = [(k, v) for k, v in meta if k not in sc.get("people", ())]
    report.cover("", tags + [("生成日期", f"{now:%Y-%m-%d}")], eyebrow=sc["eyebrow"])
    if people:
        report.table(["角色", "人员"], [[k, v] for k, v in people], col_widths=[1, 5])
    report.callout(sc["notice"], "info")
    report.callout(LEGEND, "info")
    report.heading(sc["record"])
    lines = list(_lines(blocks, speakers))
    bodies = [body for _, _, body in lines]
    # 涉及公司表里的口述写法和全称都是核对过的公司名，正文里出现了就标色，不靠模型列实体时写得一字不差
    named = [("公司", s) for c in companies for s in (*c["spoken"], c["name"]) if any(s in body for body in bodies)]
    usable = paint.check(list(dict.fromkeys([tuple(e) for e in entities or []] + named)), bodies, skipped)
    inserts, notes = _notes(blocks, origin, changes, source_name, skipped)
    reported = set()
    for b, who, body in lines:
        spans = paint.spans_of(body, usable)
        hints += paint.hints_of(body, spans, b["ord"], reported)
        # 角标位置是在整块文字里算的，说话人标签不在 body 里，所以按 body 在块里的偏移换算
        shift = b["text"].find(body)
        text = paint.render(body, spans, [(p - shift, t) for p, t in inserts.get(b["ord"], ())])
        report.paragraph(f"**{who}**：{text}" if who else text)
    if not lines:
        raise RecordError("校订后没有任何文字，不出记录")
    if notes:
        report.source_notes("数字变更说明", notes)
    if companies:
        report.heading("涉及公司")
        report.table(["口述写法", "识别为", "依据", "状态"], [["、".join(c["spoken"]), "-".join(c[k] for k in ("name", "legal", "uscc") if k in c), c["basis"], c["status"]] for c in companies], col_widths=[2, 3, 3, 1])
    report.heading("资料文件")
    report.table(["类型", "文件名"], [["录音文件", source_name]] + [["用户提供的信息文件", m] for m in materials], col_widths=[1, 4])
    return report


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def emit(stage1, stage2, changes, ctx, now=None, config=None, outdir=".", final_only=False, with_raw=True):
    """由两轮结果和改动表写出全部文件，build 和 revise 共用。ctx 是 {source, scenario, info, speakers, entities}，原样存进修订单。
    返回 {"raw", "record", "changes", "state", "skipped_marks", "unmarked_hints", "summary"}：前四个是路径（修改清单关闭时 changes 为 None）。
    final_only（修订用）只写记录 PDF 和修订单，raw、changes 为 None：原始转录不随修订变，修改清单用户不再需要。
    with_raw=False（原始转录已经提前发出）不再写原始转录。另返回 confirmations（请用户确认的事项）和 message（发给用户的说明）。"""
    cfg = config or load_config()
    now = now or datetime.now(CST)
    sc, meta = _scenario(cfg, ctx["scenario"], ctx["info"])
    source_name = os.path.basename(ctx["source"])
    stem = os.path.splitext(source_name)[0]
    suffix = f"{now:%Y%m%d-%H%M%S}"
    paths = {"raw": os.path.join(outdir, f"{stem}-原始转录-{suffix}.md"),
             "record": os.path.join(outdir, f"{stem}-{sc['record']}-{suffix}.pdf"),
             "changes": os.path.join(outdir, f"{stem}-修改清单-{suffix}.md") if cfg["emit_change_list"] and not final_only else None,
             "state": os.path.join(outdir, f"{stem}-修订单-{suffix}.json")}
    raw = None if final_only or not with_raw else raw_markdown(stage1["blocks"], stem, ctx["speakers"])
    final = [b["text"] for b in stage2["blocks"]]  # 校订后每块的文字，清单和确认问题里要按它找出所在的句子
    skipped, hints = [], []
    materials = ctx["materials"]
    if not all(isinstance(m, str) and m.strip() for m in materials):
        raise RecordError("materials 必须是文件名（非空字符串）列表，没有用户提供的信息文件就不要给")
    companies = co.check(ctx.get("companies"))
    ctx = {**ctx, "companies": companies}
    report = _record(stage2["blocks"], stage1["blocks"], ctx["entities"], ctx["speakers"], sc, meta, now, skipped, hints, changes, source_name, materials, companies)
    os.makedirs(outdir, exist_ok=True)
    if raw is not None:
        _write(paths["raw"], raw)
    else:
        paths["raw"] = None
    if paths["changes"]:
        _write(paths["changes"], ch.to_markdown(stem, changes, final))
    blocks = [{"ord": b["ord"], "text": b["original"], "anchor": b["anchor"]} for b in stage1["blocks"]]
    _write(paths["state"], json.dumps({**ctx, "blocks": blocks, "changes": changes, "final": final}, ensure_ascii=False, indent=1))
    report.save_pdf(paths["record"])  # 最后写：PDF 渲染出问题时，原始转录、修改清单和修订单已经在了
    asks = co.confirmations(changes, companies, ctx["entities"], final)[:co.LIMIT]
    return {**paths, "skipped_marks": skipped, "unmarked_hints": hints, "summary": ch.counts(changes) or "没有改动",
            "confirmations": asks, "message": co.message(changes, asks, revised=final_only, listed=bool(paths["changes"]))}


def build(stage1, stage2, entities, speakers, scenario, info, source, now=None, config=None, outdir=".", materials=(), companies=(), with_raw=True):
    """写出文件并返回 emit 的结果：raw、record、changes、state 是路径，skipped_marks 是没能标上色的项及原因，
    unmarked_hints 是可能漏标的公司，summary 是"已应用 N 处，待应用 M 处……"，回答里照它说，不要凭印象。

    entities 是 [(类型, 文字), ...]，类型是 公司、地址、人名、时间、数字，文字要在校订稿里原样出现，全文每个块出现几处标几处，
    口述的简称也要列。金额、比例、数量、日期由代码自动标。speakers 是 {"S1": "张三（访谈人）"}。
    scenario 是 config.yaml 里的场景名，info 是 {信息项: 内容}，用户给了什么项就写什么项，场景 fields 只是建议问的项和排序。
    source 是录音文件名，materials 是用户一并上传的信息文件名列表（没有就不给），两者会单独列在报告的“资料文件”里。
    报告标题是场景的记录名（访谈记录等），封面上方的英文眉题也来自场景。
    文件名是 原文件名-原始转录/记录名/修改清单/修订单-时间戳，时间按东八区。
    """
    wrong = [c for c in stage1["applied"] if c.get("kind", "fix") not in STAGE1_KINDS]
    if wrong:
        raise RecordError(f"第一轮（原始转录）只能提 {'、'.join(STAGE1_KINDS)}，错别字、热词、拆块等纠错放到第二轮；"
                          f"第一轮里有 {len(wrong)} 条不是这两种，请调用 apply_changes 时带上 kinds={STAGE1_KINDS!r}")
    ctx = {"source": source, "scenario": scenario, "info": info or {}, "speakers": speakers or {}, "materials": list(materials),
           "entities": [list(e) if isinstance(e, tuple) else e for e in entities or []], "companies": list(companies)}
    return emit(stage1, stage2, ch.collect(stage1, stage2), ctx, now, config, outdir, with_raw=with_raw)
