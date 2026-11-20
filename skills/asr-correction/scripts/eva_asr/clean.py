"""原始转录的规则清理：不经过模型，同样的输入永远得到同样的结果，所以能在转写一完成就发出。

只做四类事，且都只删字（所以都是 tidy，过得了闸门）：
0. 汉字和数字、字母之间的空格（"一个 690 万"）
1. 纯语气词 嗯、呃（"啊""那个""就是"可能有真实意义，不动；单字叠词如"看看"也不动）
2. 识别出来的乱码标记，如 "# Hm."、"Error."
3. 2 字以上的完全重复：商标商标→商标、没有，没有→没有（第二遍必须是一句话的结尾，避免"没有，没有人"被误删）
数字、公司名、错别字一概不碰，那些是第二轮校订的事。
"""
import re

from eva_asr.diff_gate import apply_changes
from eva_asr.proofread import hunks

_LABEL = re.compile(r"\A[A-Za-z0-9.]+：")
_HAN = "一-鿿"
_PUNCT = "，,、。.？?！!"
_FILLER = re.compile(rf"([{_PUNCT}]?)[嗯呃]+[，,、。.]?")  # 前面已有标点就留它，免得删出两个逗号
_GARBAGE = re.compile(r"#\s*[A-Za-z]{1,8}\.\s*|\bError\.")
_REPEAT = re.compile(rf"([{_HAN}]{{2,6}})\1")
_REPEAT_ACROSS = re.compile(rf"([{_HAN}]{{2,6}})[，,、]\1(?![{_HAN}])")
_COUNTED = "一二三四五六七八九十"  # "一个一个""一步一步"是正常说法，不是口吃
_SPACE = re.compile(rf"(?<=[{_HAN}]) +(?=[0-9A-Za-z])|(?<=[0-9A-Za-z]) +(?=[{_HAN}])")  # 转写在汉字和数字、字母之间爱加空格
_WORDLIKE = re.compile(rf"[{_HAN}A-Za-z0-9]")


def _rules():
    yield _SPACE, lambda m: ""
    yield _GARBAGE, lambda m: ""
    yield _FILLER, lambda m: m.group(1)
    yield _REPEAT_ACROSS, lambda m: m.group(1)
    yield _REPEAT, lambda m: m.group(0) if (len(m.group(1)) == 2 and m.group(1)[0] in _COUNTED) else m.group(1)


def clean_text(body):
    """一段正文按规则清理。整段只剩一个"嗯。"这类应答时保持原样，不删空。"""
    for pattern, repl in _rules():
        cleaned = pattern.sub(repl, body)
        if _WORDLIKE.search(cleaned):
            body = cleaned
    return body


def clean_changes(transcript):
    """逐块按规则顺序清理，再和原文对比，得到一条条 tidy 改动（只删字）。对比出来的改动位置唯一，按顺序重放一定对得上。"""
    out = []
    for b in transcript["blocks"]:
        label = _LABEL.match(b["text"])
        old = b["text"][label.end():] if label else b["text"]
        new = clean_text(old)
        out += [{**h, "reason": "语气词、乱码、口吃重复"} for h in hunks(b["ord"], old, new)]
    return out


def clean(transcript):
    """规则清理后的结果，和 apply_changes 的返回同形（blocks、applied、rejected），可直接当第一轮结果用。"""
    return apply_changes(transcript, clean_changes(transcript), kinds=("tidy",))
