"""语音转写：同步接口，一段不超过 max_seconds 的音频换回带时间的句子。

和对话模型走同一套约束：先在账本预扣、成功结算、失败释放；全进程共用并发闸和熔断；密钥只读环境变量。
厂商的应答形状只在这个文件里认识：整段音频返回一个大"句子"，里面是逐词的时间，这里按句末标点切成句子，
时间锚点来自厂商给的词时间，不估算。没有识别出文字是正常结果（空句子列表），不是错误。
"""
import base64
import functools
import logging
import os
import threading
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal
from pathlib import Path
from typing import Callable, Optional, Tuple

import requests
import yaml
from dotenv import load_dotenv

from eva.common import ids, logs
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import Purpose
from eva.model.breaker import Breakers
from eva.storage.ledger import open_ledger

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "configs" / "asr.yaml"
_PATH = "/services/aigc/multimodal-generation/generation"
_CENT = Decimal("0.0001")
_MILLION = Decimal(1_000_000)
_SENTENCE_END = "。？！?!；;"
_MESSAGE_CHARS = 200
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LongConfig:
    model: str
    max_seconds: int
    diarization: bool
    speaker_count_max: int
    part_max_seconds: int
    overlap_seconds: int
    snap_seconds: int
    per_file_parallel: int
    poll_interval_seconds: float
    poll_max_wait_seconds: float
    upload_timeout_seconds: float


@dataclass(frozen=True)
class AsrConfig:
    model: str
    api_key_env: str
    base_url_env: str
    pricing_version: str
    price_input: Decimal  # 元/百万 token
    price_output: Decimal
    estimate_input_tps: Decimal
    estimate_output_tps: Decimal
    max_seconds: int
    no_speech_codes: Tuple[str, ...]
    quota_statuses: Tuple[int, ...]
    quota_code_prefixes: Tuple[str, ...]
    connect_seconds: float
    read_seconds: float
    max_retries: int
    backoff_base_seconds: float
    max_delay_seconds: float
    max_in_flight: int
    queue_timeout_seconds: float
    breaker_failures: int
    breaker_probe_seconds: float
    long: LongConfig


@dataclass(frozen=True)
class Sentence:
    begin_ms: int
    end_ms: int
    text: str
    speaker: str = ""  # 说话人分离的结果（S1、S2…）；同步转写没有


@dataclass(frozen=True)
class Transcription:
    sentences: Tuple[Sentence, ...]  # 空 = 没有语音内容
    seconds: int
    input_tokens: int
    output_tokens: int
    cost_cny: Decimal


class _Failure(Exception):
    """可用性类失败。in_place=False 的不在原地重试（读超时：请求可能已经计费，再发一次会重复收费）。"""

    def __init__(self, detail: str, retry_after: Optional[float] = None, in_place: bool = True):
        super().__init__(detail)
        self.detail, self.retry_after, self.in_place = detail, retry_after, in_place


def _bad(msg: str, **details) -> EvaError:
    return EvaError(Code.MODEL_CONFIG_INVALID, msg, **details)


def _num(raw: dict, key: str, kind=float, minimum=0):
    v = raw.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < minimum or (kind is int and v != int(v)):
        raise _bad(f"asr.{key} 必须是不小于 {minimum} 的{'整数' if kind is int else '数'}", value=repr(v))
    return kind(v)


def _text(raw: dict, key: str) -> str:
    v = raw.get(key)
    if not isinstance(v, str) or not v.strip():
        raise _bad(f"asr.{key} 必须是非空字符串", value=repr(v))
    return v.strip()


def _list(raw: dict, key: str, kind) -> tuple:
    v = raw.get(key)
    if not isinstance(v, list) or not all(isinstance(x, kind) and not isinstance(x, bool) for x in v):
        raise _bad(f"asr.{key} 必须是 {kind.__name__} 的列表", value=repr(v))
    return tuple(v)


def load(path: Path = DEFAULT_PATH) -> AsrConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise _bad("读不到或解析不了转写配置", path=str(path), error=str(e)) from e
    if not isinstance(raw, dict):
        raise _bad("转写配置顶层必须是映射", path=str(path))
    section = lambda name: raw.get(name) if isinstance(raw.get(name), dict) else {}  # noqa: E731
    price, est, timeout, retry = (section(n) for n in ("price_cny_per_million_tokens", "estimate", "timeout", "retry"))
    conc, breaker, lng = section("concurrency"), section("breaker"), section("long")
    diarization = lng.get("diarization")
    if not isinstance(diarization, bool):
        raise _bad("asr.long.diarization 必须是布尔值", value=repr(diarization))
    long = LongConfig(
        model=_text(lng, "model"), max_seconds=_num(lng, "max_seconds", int, minimum=1), diarization=diarization,
        speaker_count_max=_num(lng, "speaker_count_max", int, minimum=2),
        part_max_seconds=_num(lng, "part_max_seconds", int, minimum=1), overlap_seconds=_num(lng, "overlap_seconds", int),
        snap_seconds=_num(lng, "snap_seconds", int), per_file_parallel=_num(lng, "per_file_parallel", int, minimum=1),
        poll_interval_seconds=_num(lng, "poll_interval_seconds", minimum=0.01),
        poll_max_wait_seconds=_num(lng, "poll_max_wait_seconds", minimum=1),
        upload_timeout_seconds=_num(lng, "upload_timeout_seconds", minimum=1))
    if long.overlap_seconds * 2 >= long.part_max_seconds:
        raise _bad("asr.long.overlap_seconds 的两倍必须小于 part_max_seconds", overlap=long.overlap_seconds, part_max=long.part_max_seconds)
    return AsrConfig(
        model=_text(raw, "model"), api_key_env=_text(raw, "api_key_env"), base_url_env=_text(raw, "base_url_env"),
        pricing_version=_text(raw, "pricing_version"),
        price_input=Decimal(str(_num(price, "input"))), price_output=Decimal(str(_num(price, "output"))),
        estimate_input_tps=Decimal(str(_num(est, "input_tokens_per_second", minimum=1))),
        estimate_output_tps=Decimal(str(_num(est, "output_tokens_per_second", minimum=1))),
        max_seconds=_num(raw, "max_seconds", int, minimum=1),
        no_speech_codes=_list(raw, "no_speech_codes", str),
        quota_statuses=_list(raw, "quota_statuses", int), quota_code_prefixes=_list(raw, "quota_code_prefixes", str),
        connect_seconds=_num(timeout, "connect_seconds", minimum=1), read_seconds=_num(timeout, "read_seconds", minimum=1),
        max_retries=_num(retry, "max_retries", int), backoff_base_seconds=_num(retry, "backoff_base_seconds"),
        max_delay_seconds=_num(retry, "max_delay_seconds"),
        max_in_flight=_num(conc, "max_in_flight", int, minimum=1),
        queue_timeout_seconds=_num(conc, "queue_timeout_seconds", minimum=1),
        breaker_failures=_num(breaker, "open_after_consecutive_failures", int, minimum=1),
        breaker_probe_seconds=_num(breaker, "probe_after_seconds"), long=long,
    )


def credentials(cfg: AsrConfig) -> Tuple[str, str]:
    load_dotenv()
    missing = [v for v in (cfg.api_key_env, cfg.base_url_env) if not os.environ.get(v)]
    if missing:
        raise _bad("缺少转写的环境变量", model=cfg.model, missing=missing)
    return os.environ[cfg.api_key_env], os.environ[cfg.base_url_env]


@functools.lru_cache(maxsize=None)
def _process_gate(size: int) -> threading.BoundedSemaphore:
    """进程级：所有租户的转写共用一个厂商额度。"""
    return threading.BoundedSemaphore(size)


@functools.lru_cache(maxsize=None)
def _process_breakers(failures: int, probe_after_seconds: float) -> Breakers:
    return Breakers(failures, probe_after_seconds)


def _provider_error(resp) -> Tuple[str, str]:
    try:
        body = resp.json()
        return str(body.get("code") or ""), str(body.get("message") or "")[:_MESSAGE_CHARS]
    except (ValueError, AttributeError):
        return "", ""


def _join(parts: list) -> str:
    """词之间不加空格，只有相邻的两个英文词（或英文与数字）才加，免得拼成一个词。厂商把数字逐位切成词，数字之间不能加。"""
    out = ""
    for word, punct in parts:
        a, b = out[-1:], word[:1]
        if a.isascii() and a.isalnum() and b.isascii() and b.isalnum() and not (a.isdigit() and b.isdigit()):
            out += " "
        out += word + punct
    return out


def sentences_from(words: list) -> Tuple[Sentence, ...]:
    """逐词（带标点）切成句子。一句的起止时间是它第一个词的开始和最后一个词的结束。"""
    sentences, parts, begin = [], [], 0
    for w in words:
        if not parts:
            begin = w["begin_time"]
        parts.append((w["text"], w.get("punctuation") or ""))
        if parts[-1][1] and parts[-1][1][-1] in _SENTENCE_END:
            sentences.append(Sentence(begin, w["end_time"], _join(parts)))
            parts = []
    if parts:
        sentences.append(Sentence(begin, words[-1]["end_time"], _join(parts)))
    return tuple(sentences)


class Asr:
    def __init__(self, tenant_id: str, cfg: AsrConfig, budget_cny, http=None, open_ledger_fn: Callable = open_ledger,
                 breakers: Optional[Breakers] = None, gate: Optional[threading.BoundedSemaphore] = None,
                 sleep: Callable[[float], None] = time.sleep):
        if not isinstance(tenant_id, str) or not tenant_id:
            raise EvaError(Code.TENANT_REQUIRED, "转写必须带租户")
        self.tenant_id, self.cfg, self._budget = tenant_id, cfg, budget_cny
        self._http, self._open_ledger, self._sleep = http or requests.Session(), open_ledger_fn, sleep
        self._breaker = (breakers or _process_breakers(cfg.breaker_failures, cfg.breaker_probe_seconds)).get(cfg.model)
        self._gate = gate or _process_gate(cfg.max_in_flight)

    def transcribe(self, session_id: str, audio: bytes, fmt: str, seconds: float) -> Transcription:
        """audio 是 fmt 格式的完整音频字节，seconds 是它的时长。超过 max_seconds 是调用方的错，抛 E0001。"""
        if not audio or not 0 < seconds <= self.cfg.max_seconds:
            raise EvaError(Code.INVALID_VALUE, "音频为空或时长超出同步转写上限", seconds=seconds, max_seconds=self.cfg.max_seconds)
        call_id, last, attempts = ids.new_id(), None, 0
        for retry in range(self.cfg.max_retries + 1):
            if not self._breaker.allow():
                raise last or EvaError(Code.MODEL_UNAVAILABLE, "转写熔断开启，暂不请求", model=self.cfg.model)
            attempts += 1
            started = time.monotonic()
            try:
                result = self._attempt(session_id, f"{call_id}:{attempts}", audio, fmt, seconds)
            except _Failure as f:
                self._breaker.failure()
                logs.emit(log, logging.WARNING, "转写失败", model=self.cfg.model, detail=f.detail, attempt=attempts,
                          seconds=round(time.monotonic() - started, 1), retrying=f.in_place and retry < self.cfg.max_retries)
                last = EvaError(Code.MODEL_UNAVAILABLE, "转写服务不可用", model=self.cfg.model, reason=f.detail, attempts=attempts)
                if not f.in_place:
                    raise last from f
                if retry < self.cfg.max_retries:
                    self._sleep(min(max(self.cfg.backoff_base_seconds * 2 ** retry, f.retry_after or 0), self.cfg.max_delay_seconds))
                continue
            except EvaError as e:
                if e.code is not Code.MODEL_BUSY:  # 排队超时没碰过服务
                    self._breaker.success()
                raise
            self._breaker.success()
            logs.emit(log, logging.INFO, "转写完成", model=self.cfg.model, audio_seconds=seconds, attempts=attempts,
                      sentences=len(result.sentences), tokens_in=result.input_tokens, tokens_out=result.output_tokens,
                      seconds=round(time.monotonic() - started, 1))
            return result
        raise last

    def _attempt(self, session_id: str, idem: str, audio: bytes, fmt: str, seconds: float) -> Transcription:
        cfg = self.cfg
        key, base = credentials(cfg)
        with self._open_ledger(self.tenant_id) as led:
            hold_id = led.hold(session_id, idem, Purpose.ASR.value, self._estimate(seconds), cfg.pricing_version,
                               model=cfg.model, budget_cny=self._budget)
        try:
            body = {"model": cfg.model, "parameters": {"format": fmt},
                    "input": {"messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {
                        "data": f"data:audio/{fmt};base64,{base64.b64encode(audio).decode('ascii')}"}}]}]}}
            if not self._gate.acquire(timeout=cfg.queue_timeout_seconds):
                raise EvaError(Code.MODEL_BUSY, "转写请求排队超时", model=cfg.model, max_in_flight=cfg.max_in_flight,
                               waited_seconds=cfg.queue_timeout_seconds)
            try:
                resp = self._http.post(base.rstrip("/") + _PATH, headers={"Authorization": f"Bearer {key}"}, json=body,
                                       timeout=(cfg.connect_seconds, cfg.read_seconds))
            except requests.ReadTimeout as e:
                raise _Failure(f"读超时 {type(e).__name__}", in_place=False) from e
            except requests.RequestException as e:
                raise _Failure(f"连接失败 {type(e).__name__}") from e
            finally:
                self._gate.release()
            result = self._parse(resp, seconds)
        except BaseException:
            with self._open_ledger(self.tenant_id) as led:
                led.release(hold_id, idem)
            raise
        if result is None:  # 没有语音内容：厂商不计费，预扣还回去
            with self._open_ledger(self.tenant_id) as led:
                led.release(hold_id, idem)
            return Transcription((), round(seconds), 0, 0, Decimal(0))
        with self._open_ledger(self.tenant_id) as led:
            led.settle(hold_id, idem, result.cost_cny, cfg.pricing_version, model=cfg.model,
                       tokens_in=result.input_tokens, tokens_out=result.output_tokens, asr_seconds=result.seconds)
        return result

    def _estimate(self, seconds: float) -> Decimal:
        c = self.cfg
        return ((Decimal(seconds) * c.estimate_input_tps * c.price_input + Decimal(seconds) * c.estimate_output_tps * c.price_output)
                / _MILLION).quantize(_CENT, ROUND_UP)

    def _parse(self, resp, seconds: float) -> Optional[Transcription]:
        """None 表示没有语音内容。出处锚点（词的起止时间）缺失的应答不往下传，当作瞬时失败。"""
        cfg, status = self.cfg, resp.status_code
        if status != 200:
            code, message = _provider_error(resp)
            if status == 400 and code in cfg.no_speech_codes:
                return None
            if status in cfg.quota_statuses or any(code.startswith(p) for p in cfg.quota_code_prefixes):
                raise EvaError(Code.MODEL_QUOTA_EXHAUSTED, "转写额度耗尽", model=cfg.model, status=status, error_code=code)
            if status in (401, 403):
                raise EvaError(Code.MODEL_AUTH, "转写鉴权失败", model=cfg.model, status=status, error_code=code)
            if status == 429 or status >= 500 or status == 408:
                header = resp.headers.get("retry-after") if getattr(resp, "headers", None) else None
                try:
                    retry_after = float(header) if header else None
                except ValueError:
                    retry_after = None
                raise _Failure(f"{status} {code}", retry_after)
            raise EvaError(Code.MODEL_REQUEST_REJECTED, "转写请求被厂商拒绝", model=cfg.model, status=status,
                           error_code=code, reason=message)
        try:
            body = resp.json()
            usage = body["usage"]
            tokens_in, tokens_out = int(usage["input_tokens"]), int(usage["output_tokens"])
            sentence = body["sentence"]
            words = sentence["words"]
            valid = tokens_in >= 0 and tokens_out >= 0 and isinstance(words, list) and all(
                isinstance(w, dict) and isinstance(w.get("text"), str) and w["text"]
                and all(isinstance(w.get(k), int) and not isinstance(w.get(k), bool) and w[k] >= 0 for k in ("begin_time", "end_time"))
                and w["end_time"] >= w["begin_time"] for w in words)
        except (ValueError, KeyError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise _Failure("应答缺少 usage 或逐词时间，或内容自相矛盾")
        if not words:
            return None
        cost = ((tokens_in * cfg.price_input + tokens_out * cfg.price_output) / _MILLION).quantize(_CENT, ROUND_HALF_UP)
        return Transcription(sentences_from(words), round(seconds), tokens_in, tokens_out, cost)
