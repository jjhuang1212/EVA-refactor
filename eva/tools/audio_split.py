"""标准 wav 过长时切段：均匀切，每段末尾多带 overlap 进下一段开头，切点吸附到句间停顿。

只处理我们自己在沙箱里转出来的 16 位单声道 wav（纯采样点搬运，不解码不可信的文件，所以在宿主做）。
重叠区会被相邻两段各转写一遍，speakers.py 靠它把各段的说话人编号对齐、去掉重复的句子。
"""
import audioop
import io
import math
import wave
from dataclasses import dataclass
from typing import List, Optional

from eva.contracts.errors import Code, EvaError

_WIDTH = 2
_BLOCK_MS = 20  # 找停顿时的分析单位
_MIN_SILENCE_MS = 300  # 短于它的不算句间停顿
_RMS_FLOOR = 300  # 静音阈值的绝对下限


@dataclass(frozen=True)
class Part:
    index: int
    offset_ms: int  # 本段起点在整份录音里的位置
    seconds: float
    wav: bytes


def _wav_bytes(rate: int, frames: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(_WIDTH)
        f.setframerate(rate)
        f.writeframes(frames)
    return buf.getvalue()


def _longest_pause_center(f: "wave.Wave_read", lo_ms: int, hi_ms: int) -> Optional[int]:
    """[lo_ms, hi_ms) 内最长停顿的中点（毫秒）。阈值取窗口内 20ms 块 RMS 的 15 分位 ×1.5，不同录音的增益差一个量级时仍稳；
    找不到不短于 300ms 的停顿返回 None。"""
    rate = f.getframerate()
    lo, hi = max(0, lo_ms * rate // 1000), min(f.getnframes(), hi_ms * rate // 1000)
    if hi <= lo:
        return None
    block = max(1, _BLOCK_MS * rate // 1000)
    f.setpos(lo)
    blocks, pos = [], lo
    while pos < hi:
        pcm = f.readframes(min(block, hi - pos))
        if not pcm:
            break
        blocks.append((pos, len(pcm) // _WIDTH, audioop.rms(pcm, _WIDTH)))
        pos += len(pcm) // _WIDTH
    if not blocks:
        return None
    ordered = sorted(b[2] for b in blocks)
    threshold = max(_RMS_FLOOR, ordered[len(ordered) * 15 // 100] * 1.5)
    best, run_start, run = (0, 0), None, 0
    for start, n, rms in blocks + [(0, 0, float("inf"))]:  # 哨兵收尾最后一段
        if rms < threshold:
            if run_start is None:
                run_start, run = start, 0
            run += n
        else:
            if run_start is not None and run > best[1]:
                best = (run_start, run)
            run_start = None
    if best[1] * 1000 < _MIN_SILENCE_MS * rate:
        return None
    return (best[0] + best[1] // 2) * 1000 // rate


def split(wav: bytes, part_max_seconds: int, overlap_seconds: int, snap_seconds: int) -> List[Part]:
    """不超过 part_max_seconds 的直接返回一段。否则按净长预算均分：
    净长 = 上限 - 重叠 - 两侧各一个吸附窗口，保证吸附后加上重叠，任何一段都不超过上限。"""
    with wave.open(io.BytesIO(wav), "rb") as f:
        rate, total, ch, width = f.getframerate(), f.getnframes(), f.getnchannels(), f.getsampwidth()
        if (ch, width) != (1, _WIDTH):
            raise EvaError(Code.INVALID_VALUE, "切段只接受单声道 16 位 wav", channels=ch, width=width)
        total_ms = total * 1000 // rate
        if total_ms <= part_max_seconds * 1000:
            return [Part(0, 0, total / rate, wav)]
        snap_ms, overlap_ms = snap_seconds * 1000, overlap_seconds * 1000
        net = part_max_seconds * 1000 - overlap_ms - 2 * snap_ms
        if net <= min(snap_ms, 60_000):  # 吸附窗口塞不下时放弃吸附，不切出超限的段
            snap_ms, net = 0, part_max_seconds * 1000 - overlap_ms
        if net <= 0:
            raise EvaError(Code.CONFIG_INVALID, "重叠时长不小于切段上限", overlap_seconds=overlap_seconds, part_max_seconds=part_max_seconds)
        count = math.ceil(total_ms / net)
        cuts = [0]
        for i in range(1, count):
            ideal = round(i * total_ms / count)
            center = _longest_pause_center(f, ideal - snap_ms, ideal + snap_ms) if snap_ms else None
            cuts.append((ideal if center is None else center) * rate // 1000)
        cuts.append(total)
        parts = []
        for i in range(count):
            f.setpos(cuts[i])
            want = cuts[i + 1] - cuts[i] + (0 if i == count - 1 else overlap_seconds * rate)
            frames = f.readframes(want)
            parts.append(Part(i, cuts[i] * 1000 // rate, len(frames) / (rate * _WIDTH), _wav_bytes(rate, frames)))
        return parts
