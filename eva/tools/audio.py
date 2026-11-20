"""录音 → 带时间段锚点的文字块：先在解析沙箱里转成标准 wav，再交给转写，结果按文件缓存。

转写要联网和密钥，只能在宿主做；解码不可信的媒体文件只能在沙箱做。两步中间的 wav 不落库。
缓存键 = 租户内容哈希 + 这里的版本（转写模型、预处理参数、分块参数和本模块与预处理的源码），
和文档解析的版本分开：改了某个文档解析器不该让已经花钱转写过的录音重转。
没有语音内容（静音，或转写返回空）是正常结果：空块列表，warnings 里写明，调用方据此告诉用户。
"""
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from decimal import Decimal
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

import eva
from eva.common import logs
from eva.contracts.document import AudioLimits, Block, Parsed
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import BlockKind
from eva.model.asr import Asr, AsrConfig, Sentence
from eva.model.filetrans import Filetrans
from eva.parsing.builder import split_text
from eva.storage.objects import ObjectStore, Objects
from eva.tools import audio_split, speakers
from eva.tools.documents import Documents

log = logging.getLogger(__name__)
WAV_HEADER_BYTES = 44
NO_SPEECH = {"reason": "no_speech", "message": "这段录音里没有识别出说话内容"}
SPEAKER_COUNT_IGNORED = {"reason": "speaker_count_ignored", "message": "这条录音没有走分说话人的转写（时长较短或没开分离），指定的人数没有用上"}


def blocks_from(sentences: Sequence[Sentence], max_seconds: int, max_chars: int) -> Tuple[Block, ...]:
    """相邻的句子攒成块：加上下一句会超过时长或字符上限、或说话人换了就另起一块。每块的锚点是它首句开始到末句结束，
    有说话人的块文字以"S1："开头。单句本身超过字符上限时硬切，切出的各片共用这一句的时间段。"""
    groups: List[List[Sentence]] = []
    for s in sentences:
        cur = groups[-1] if groups else None
        if (cur and s.speaker == cur[0].speaker and s.end_ms - cur[0].begin_ms <= max_seconds * 1000
                and sum(len(x.text) for x in cur) + len(s.text) <= max_chars):
            cur.append(s)
        else:
            groups.append([s])
    blocks = []
    for g in groups:
        begin, end, who = g[0].begin_ms, g[-1].end_ms, g[0].speaker
        anchor = {"element": "audio", "time_ms": [begin, end], "asr": True, **({"speaker": who} if who else {})}
        for piece in split_text("".join(s.text for s in g), max_chars):
            if piece.strip():
                blocks.append(Block(len(blocks), BlockKind.TRANSCRIPT, f"{who}：{piece}" if who else piece, dict(anchor), ()))
    return tuple(blocks)


class Audio:
    def __init__(self, documents: Documents, make_asr: Callable[[str], Asr], asr_cfg: AsrConfig,
                 make_long: Optional[Callable[[str], Filetrans]] = None):
        """make_long 为空时只收同步转写能接受的时长，更长的报 too_long。"""
        self._documents, self._make_asr, self._make_long, self._asr_cfg = documents, make_asr, make_long, asr_cfg
        cfg = documents.cfg
        self.extensions, self.max_file_bytes = cfg.audio.extensions, cfg.max_file_bytes
        ceiling = asr_cfg.long.max_seconds if make_long else asr_cfg.max_seconds
        self._limits = AudioLimits(ceiling, cfg.audio.sample_rate, cfg.audio.target_peak_db,
                                   cfg.audio.max_gain_db, cfg.audio.silence_peak_db)
        wav_bytes = ceiling * cfg.audio.sample_rate * 2 + WAV_HEADER_BYTES
        if wav_bytes > cfg.sandbox.max_file_bytes:
            raise EvaError(Code.CONFIG_INVALID, "解析沙箱的单文件上限容不下最长录音转出的 wav，超过约 10 分钟的录音都会转码失败",
                           wav_bytes=wav_bytes, max_file_bytes=cfg.sandbox.max_file_bytes, max_seconds=ceiling)
        self._block_chars, self._block_seconds = cfg.limits.block_max_chars, cfg.audio.block_max_seconds
        root = Path(eva.__file__).resolve().parent
        digest = hashlib.sha256(json.dumps([asr_cfg.model, vars(asr_cfg.long) if make_long else None, vars(self._limits),
                                            self._block_chars, self._block_seconds], sort_keys=True).encode())
        for path in (root / "parsing" / "audio.py", Path(__file__), Path(audio_split.__file__), Path(speakers.__file__)):
            digest.update(path.read_bytes())
        self.version = digest.hexdigest()[:12]
        self.speaker_count_max = asr_cfg.long.speaker_count_max if make_long else None

    def version_for(self, speaker_count: Optional[int]) -> str:
        """指定了人数的转写是另一份结果，缓存键里带上人数；不指定就是默认版本。"""
        if speaker_count is None:
            return self.version
        return hashlib.sha256(f"{self.version}:{speaker_count}".encode()).hexdigest()[:12]

    def transcribe(self, tenant_id: str, session_id: str, filename: str, data: bytes, cancel,
                   speaker_count: Optional[int] = None) -> Parsed:
        """解不开、太长抛 E2002；转写服务的故障抛 E5xxx；没有语音内容返回空块列表加 no_speech 警告。
        speaker_count 只在分说话人的长录音转写里用得上，用不上时在 warnings 里说明。"""
        version = self.version_for(speaker_count)
        started = time.monotonic()
        wav, info = self._documents.prepare_audio(tenant_id, filename, data, self._limits, cancel)
        if wav is None:
            return self._empty(info["seconds"], "静音", started, version)
        warnings = ()
        if info["seconds"] <= self._asr_cfg.max_seconds:
            result = self._make_asr(tenant_id).transcribe(session_id, wav, "wav", info["seconds"])
            sentences, cost = result.sentences, result.cost_cny
        else:
            sentences, cost, unaligned = self._transcribe_long(tenant_id, session_id, wav, cancel, speaker_count)
            if unaligned:
                warnings = ({"reason": "speaker_unaligned", "speakers": unaligned,
                             "message": "切段转写时这些说话人没能和前一段对上号，编号只在各自那一段内有效"},)
        if speaker_count is not None and not (info["seconds"] > self._asr_cfg.max_seconds and self._asr_cfg.long.diarization):
            warnings += (dict(SPEAKER_COUNT_IGNORED),)
        blocks = blocks_from(sentences, self._block_seconds, self._block_chars)
        if not blocks:
            return self._empty(info["seconds"], "转写为空", started, version)
        logs.emit(log, logging.INFO, "录音转写完成", file=filename, audio_seconds=round(info["seconds"], 1),
                  gain_db=round(info["gain_db"], 1), blocks=len(blocks), cost=str(cost),
                  speakers=len({s.speaker for s in sentences if s.speaker}), seconds=round(time.monotonic() - started, 1))
        return Parsed("audio", version, blocks, warnings)

    def _transcribe_long(self, tenant_id: str, session_id: str, wav: bytes, cancel, speaker_count):
        """长录音：必要时切段（带重叠），各段并行提交，再按偏移拼回整条时间轴。返回（句子, 总金额, 没能对齐的说话人）。"""
        lc = self._asr_cfg.long
        parts = audio_split.split(wav, lc.part_max_seconds, lc.overlap_seconds, lc.snap_seconds)
        long = self._make_long(tenant_id)
        with ThreadPoolExecutor(max_workers=min(lc.per_file_parallel, len(parts))) as pool:
            results = list(pool.map(lambda p: long.transcribe(session_id, p.wav, p.seconds, cancel, speaker_count), parts))
        sentences, unaligned = speakers.merge(parts, [r.sentences for r in results], lc.overlap_seconds * 1000)
        return sentences, sum((r.cost_cny for r in results), Decimal(0)), unaligned

    def _empty(self, seconds: float, why: str, started: float, version: str) -> Parsed:
        logs.emit(log, logging.INFO, "录音没有语音内容", why=why, audio_seconds=round(seconds, 1),
                  seconds=round(time.monotonic() - started, 1))
        return Parsed("audio", version, (), (dict(NO_SPEECH),))


def load_transcript(audio: Audio, store: ObjectStore, open_tx: Callable[[], AbstractContextManager],
                    tenant_id: str, session_id: str, file_id: str, cancel, speaker_count: Optional[int] = None) -> Tuple[dict, Parsed]:
    """返回（登记行, 转写结果）。先查缓存；事务只包存储读写，转写期间不占着数据库连接。
    不同人数的转写各存一份，"最近一次解析"始终指向刚读的这份，引用校验按它核对块号。"""
    version = audio.version_for(speaker_count)
    with open_tx() as conn:
        objects = Objects(conn, tenant_id, store)
        row = objects.describe(file_id)
        cached = objects.read_parsed(file_id, version)
        if cached is None:
            _, data = objects.read_original(file_id, audio.max_file_bytes)
        else:
            objects.point_parsed(file_id, version)
    if cached is not None:
        parsed = Parsed.from_dict(json.loads(cached))
        logs.emit(log, logging.INFO, "转写缓存命中", file=row["filename"], blocks=len(parsed.blocks))
        return row, parsed
    parsed = audio.transcribe(tenant_id, session_id, row["filename"], data, cancel, speaker_count)
    if parsed.blocks:  # 空结果不缓存：静音便宜，转写返回空也不收费，重来一次不亏，也不把一次偶然的空结果固定下来
        with open_tx() as conn:
            Objects(conn, tenant_id, store).put_parsed(file_id, version,
                                                        json.dumps(parsed.to_dict(), ensure_ascii=False).encode("utf-8"))
    return row, parsed
