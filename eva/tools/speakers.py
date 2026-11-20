"""切段转写的结果拼回整条时间轴：加偏移、说话人编号对齐、重叠区去重。

说话人分离是在单次任务内部判的，第 0 段的 S1 和第 1 段的 S1 不保证是同一个人。相邻两段的重叠区被各转写一遍，
时间重叠且文字相近的两句就是"同一次说话的两份记录"，它们的说话人编号互为锚点；映射沿相邻段链式传导。
始终找不到锚点的说话人不猜，保留成 "p{段号}.S{n}" 并在警告里列出。
"""
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Dict, List, Sequence, Set, Tuple

from eva.model.asr import Sentence
from eva.tools.audio_split import Part

# 同一次说话的判据：时间重叠占较短者的比例，和文字相似度。两段对同一句话给的起止时间会差几十毫秒，所以重叠比不取太高
_OVERLAP = 0.5
_SIMILAR = 0.55
# 断句粒度不一致（一段给一个长句、另一段给三个短句）时严格配对会漏，用更松的判据再丢一轮；只用于去重，不做说话人锚点
_LOOSE_OVERLAP = 0.7
_LOOSE_SIMILAR = 0.4


@dataclass
class _Seg:
    begin: int
    end: int
    text: str
    speaker: str
    part: int


def _overlap_ratio(a: _Seg, b: _Seg) -> float:
    shorter = min(a.end - a.begin, b.end - b.begin)
    return (min(a.end, b.end) - max(a.begin, b.begin)) / shorter if shorter > 0 else 0.0


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _pairs(prev: List[_Seg], cur: List[_Seg]) -> List[Tuple[_Seg, _Seg]]:
    """一对一贪心配对（按 重叠×相似度 降序，每句只配一次）：重叠区里两人说同音短句时，
    真重复的那一对分数必然更高，先占掉，假对因一方已被占用而作废。"""
    cands = []
    for a in prev:
        for b in cur:
            ratio = _overlap_ratio(a, b)
            if ratio >= _OVERLAP:
                sim = _similar(a.text, b.text)
                if sim >= _SIMILAR:
                    cands.append((ratio * sim, id(a), id(b), a, b))
    used, out = set(), []
    for _, ia, ib, a, b in sorted(cands, key=lambda c: c[:3], reverse=True):
        if ia not in used and ib not in used:
            used.update((ia, ib))
            out.append((a, b))
    return out


def merge(parts: Sequence[Part], per_part: Sequence[Sequence[Sentence]], overlap_ms: int) -> Tuple[Tuple[Sentence, ...], List[str]]:
    """parts[i] 的句子时间相对本段起点。返回（整条时间轴上的句子，没能对齐的说话人）。"""
    if len(parts) != len(per_part):
        raise ValueError(f"段数 {len(parts)} 与结果数 {len(per_part)} 不一致")
    by_part: List[List[_Seg]] = [[_Seg(s.begin_ms + p.offset_ms, s.end_ms + p.offset_ms, s.text, s.speaker, p.index) for s in sents]
                                 for p, sents in zip(parts, per_part)]
    mapping, unresolved = _speaker_map(by_part, parts, overlap_ms)
    dropped: Set[int] = set()
    for i in range(1, len(parts)):
        prev, cur = _window(by_part[i - 1], by_part[i], parts[i], overlap_ms)
        for a, b in _pairs(prev, cur):
            dropped.add(id(b))
            a.end = max(a.end, b.end)  # 前段在重叠中点截断的尾音可能比后段记的短
        for b in cur:  # 粒度兜底：一个前段长句可以对应多个后段短句，前段一侧不要求唯一
            if id(b) not in dropped:
                for a in prev:
                    if _overlap_ratio(a, b) >= _LOOSE_OVERLAP and _similar(a.text, b.text) >= _LOOSE_SIMILAR:
                        a.end = max(a.end, b.end)
                        dropped.add(id(b))
                        break
    kept = [s for seg in by_part for s in seg if id(s) not in dropped]
    kept.sort(key=lambda s: (s.begin, s.end))
    return (tuple(Sentence(s.begin, s.end, s.text, mapping.get((s.part, s.speaker), s.speaker)) for s in kept), unresolved)


def _window(prev: List[_Seg], cur: List[_Seg], part: Part, overlap_ms: int) -> Tuple[List[_Seg], List[_Seg]]:
    """只比较重叠区里的句子，免得长录音上做全量两两比较。窗口放宽一个重叠时长，容纳切点吸附带来的偏差。"""
    lo, hi = part.offset_ms - overlap_ms, part.offset_ms + 2 * overlap_ms
    return [s for s in prev if s.end > lo], [s for s in cur if s.begin < hi]


def _speaker_map(by_part: List[List[_Seg]], parts: Sequence[Part], overlap_ms: int) -> Tuple[Dict[Tuple[int, str], str], List[str]]:
    parent: Dict[Tuple[int, str], Tuple[int, str]] = {}

    def find(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    for i in range(1, len(parts)):
        prev, cur = _window(by_part[i - 1], by_part[i], parts[i], overlap_ms)
        for a, b in _pairs(prev, cur):
            if a.speaker and b.speaker:
                ra, rb = find((i - 1, a.speaker)), find((i, b.speaker))
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)  # 段号小的做代表，第 0 段的编号是全局编号的基底
    mapping, unresolved = {}, set()
    for seg in (s for part in by_part for s in part):
        if seg.speaker and (seg.part, seg.speaker) not in mapping:
            root = find((seg.part, seg.speaker))
            if root[0] == 0:
                mapping[(seg.part, seg.speaker)] = root[1]
            else:
                mapping[(seg.part, seg.speaker)] = f"p{root[0] + 1}.{root[1]}"
                unresolved.add(mapping[(seg.part, seg.speaker)])
    return mapping, sorted(unresolved)
