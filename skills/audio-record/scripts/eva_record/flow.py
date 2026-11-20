"""录音记录的两步流程：prepare 在转写一出来就生成原始转录（规则清理，不等模型），并写出校订的输入；
finish 把 proofread 工具的校订结果做完闸门检查，出最终记录。

两步之间不传中间状态：规则清理是确定的，finish 对同一份转写稿重算一遍得到同样的块，校订结果里的块号和它对得上。校订前不合并相邻同一人的块：短促的应答并进长句，模型会当噪音删掉。
"""
import json
import os
from datetime import datetime

from eva_asr import UNLISTED, apply_changes, clean, clean_text, load_transcript, proofread_changes

from eva_record import changes as ch
from eva_record.build import CST, STAGE1_KINDS, _write, emit, raw_markdown
from eva_record.errors import RecordError


def prepare(transcript_path, speakers, source, now=None, outdir="."):
    """写出原始转录 md 和校订输入 json，返回 {"raw": 路径, "proofread_input": 路径, "blocks": 块数, "merged_blocks": 合并后的块数}。
    原始转录拿到路径后马上 write_file、deliver_file，用户不用等校订。校订输入只 write_file（拿到 file_id 交给 proofread 工具），不交付。
    speakers 是 {"S1": "张三（投资经理）"}，source 是录音文件名，文件名是 原文件名-原始转录-时间戳.md。"""
    first = clean(load_transcript(transcript_path))
    stem = os.path.splitext(os.path.basename(source))[0]
    path = os.path.join(outdir, f"{stem}-原始转录-{(now or datetime.now(CST)):%Y%m%d-%H%M%S}.md")
    os.makedirs(outdir, exist_ok=True)
    _write(path, raw_markdown(first["blocks"], stem, speakers))
    src = os.path.join(outdir, f"{stem}-校订输入.json")
    with open(src, "w", encoding="utf-8") as f:
        json.dump({"source": source, "speakers": speakers or {}, "blocks": [{"ord": b["ord"], "text": b["text"]} for b in first["blocks"]]}, f, ensure_ascii=False)
    return {"raw": path, "proofread_input": src, "blocks": len(first["blocks"])}


def _load_corrected(corrected):
    """校订结果：{块号: 文字}，或 proofread 工具存下来的结果文件路径（多个就按顺序，后面的覆盖前面的：重做的块在后面）。
    返回 (corrected, entities, missing)：missing 是工具没拿到结果的块号。"""
    if isinstance(corrected, dict):
        return corrected, [], []
    out, entities, missing = {}, [], set()
    for path in [corrected] if isinstance(corrected, str) else list(corrected):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            out.update({int(k): v for k, v in data["corrected"].items()})
            entities += [tuple(e) for e in data.get("entities", [])]
            missing |= set(data.get("missing", []))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            raise RecordError(f"校订结果文件读不了或格式不对：{path}（{e}）") from e
    return out, entities, sorted(missing - set(out))


def finish(transcript_path, corrected, speakers, scenario, info, source, entities=None, materials=(), companies=(), now=None, config=None, outdir="."):
    """出最终记录 PDF、修订单（和开发配置下的修改清单）。不再写原始转录，它已经由 prepare 提前发出。

    corrected 是 proofread 工具的结果文件路径（重做过的块有第二个文件就写成列表，后面的覆盖前面的），或 {块号: 改好后的文字}：
    一段文字就是这一块改好后的正文，正文前写 "标签：" 且和原来不同是改说话人，几行 "标签：文字" 是拆块。
    校订完成后，相邻同一人的块再合并。
    纠错、修顺都可以，但不缩写、不补写、不丢数字：整块字数不能缩成摘要或膨胀，保留的原话不能太少，不过的整块还原。
    提交的文字会先按原始转录同样的规则清一遍语气词和口吃，再和原始转录对比，每处改动都过闸门。
    entities 是 [(类型, 文字), ...] 标色用，不给就用校订结果里的；companies 是涉及公司表，每行
    {"spoken": [口述写法], "name": 全称, "basis": 依据, "status": "已确认"或"待确认"}。
    返回 emit 的结果：record、state、changes 是路径，message 是发给用户的说明（照着转述），confirmations 是请用户确认的事项，
    refused 是被还原的块和原因（只给你看，不会列给用户；校订没拿到结果的块也在里面），skipped_marks 和 unmarked_hints 是标色上的问题。
    """
    transcript = load_transcript(transcript_path)
    first = clean(transcript)
    if any(c.get("kind") not in STAGE1_KINDS for c in first["applied"]):
        raise RecordError("规则清理只会产生整理，这里不应该出现其他类型")
    texts, found, missing = _load_corrected(corrected)
    changes, refused = proofread_changes(first["blocks"], texts, normalize=clean_text)
    second = apply_changes(first, changes)
    marks = list(dict.fromkeys([tuple(e) for e in entities or []] + found))
    ctx = {"source": source, "scenario": scenario, "info": info or {}, "speakers": speakers or {}, "materials": list(materials),
           "entities": [list(e) for e in marks], "companies": list(companies)}
    out = emit(first, second, ch.collect(first, second), ctx, now, config, outdir, with_raw=False)
    return {**out, "refused": refused + [{"block": b, "reason": "校订没有拿到这一块的结果，保持原样"} for b in missing]
            + [{"block": r["change"].get("block"), "reason": r["reason"]} for r in second["rejected"] if r["code"] in UNLISTED]}
