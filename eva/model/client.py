"""模型调用层：所有模型调用的唯一入口，也是唯一持厂商 SDK 的地方。

每次尝试（含失败的）都先预扣，成功结算，失败释放；预扣和结算各自独立提交，先于模型请求落库。
这一层只处理不改变请求内容的失败（重试、退避、切备选、熔断、超时、记账），
需要改请求的失败（上下文溢出、输出被截断）分类后交给循环。
"""
import functools
import hashlib
import json
import logging
import random
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Callable, Dict, List, Optional

import openai

from eva.common import clock, ids, logs
from eva.common.version import code_version
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import Purpose
from eva.model import config as cfg_mod
from eva.model.breaker import Breakers
from eva.model.config import ModelConfig, ModelSpec
from eva.storage.ledger import open_ledger

_CENT = Decimal("0.0001")
_MILLION = Decimal(1_000_000)
_TRANSIENT_STATUSES = (408, 429)
log = logging.getLogger(__name__)


@dataclass
class ModelResult:
    content: Optional[str]
    tool_calls: List[dict]  # [{"id", "name", "arguments"}]，arguments 是模型给的原始 JSON 串
    finish_reason: str  # "length" 表示输出被截断，由循环决定怎么办
    model: str  # 实际应答的模型，备选可能顶上
    tokens_in: int
    tokens_out: int
    cache_hit_tokens: int
    cost_cny: Decimal
    attempts: int  # 本次调用共尝试几次（含失败的）
    reasoning: Optional[str] = None  # 模型的思考内容，超过配置上限的部分截掉；没有或不记录为 None
    reasoning_chars: int = 0  # 思考内容截断前的字数
    seconds: float = 0.0  # 成功的那次请求的耗时（含思考和输出，非流式分不开）


class _Failure(Exception):
    """可用性类失败：重试或切备选能解决，用完才升级成 EvaError。"""

    def __init__(self, kind: str, detail: str, retry_after: Optional[float] = None):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail, self.retry_after = kind, detail, retry_after


def _dig(data: dict, path: str):
    for part in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def _error_fields(e: openai.APIStatusError):
    body = e.body if isinstance(e.body, dict) else {}
    inner = body.get("error") if isinstance(body.get("error"), dict) else body
    return str(inner.get("code") or ""), str(inner.get("message") or e.message or "")


def classify(e: Exception, spec: ModelSpec):
    """把 SDK 异常归类。认不出的异常返回 None，由调用方原样抛出，不在这里吞掉。"""
    if isinstance(e, openai.APIConnectionError):  # 含超时
        return _Failure("transient", f"{type(e).__name__}: {e}")
    if not isinstance(e, openai.APIStatusError):
        return None
    status, (code, message) = e.status_code, _error_fields(e)
    if status in spec.quota_statuses or any(code.startswith(p) for p in spec.quota_code_prefixes):
        return _Failure("quota", f"{status} {code}")
    if status in _TRANSIENT_STATUSES or status >= 500:
        header = e.response.headers.get("retry-after") if e.response is not None else None
        try:
            retry_after = float(header) if header else None
        except ValueError:
            retry_after = None
        return _Failure("transient", f"{status} {code}", retry_after)
    if status in (401, 403):
        return EvaError(Code.MODEL_AUTH, "鉴权失败", model=spec.name, status=status, error_code=code)
    if code in spec.overflow_codes or any(m in message for m in spec.overflow_message_markers):
        return EvaError(Code.MODEL_CONTEXT_OVERFLOW, "输入超出上下文", model=spec.name, status=status)
    return EvaError(Code.MODEL_REQUEST_REJECTED, "请求被厂商拒绝", model=spec.name, status=status,
                    error_code=code, reason=message)


def _body(spec: ModelSpec, thinking: bool) -> dict:
    return {**spec.extra_body, **spec.thinking_extra_body} if thinking else spec.extra_body


class ModelClient:
    def __init__(
        self,
        tenant_id: str,
        config: ModelConfig,
        breakers: Optional[Breakers] = None,
        open_ledger_fn: Optional[Callable[[], AbstractContextManager]] = None,
        client_factory: Optional[Callable[[ModelSpec], object]] = None,
        gate: Optional[threading.BoundedSemaphore] = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        now: Callable[[], datetime] = clock.now,
        recorder: Optional[Callable[[str, str, dict, Dict[str, str]], None]] = None,
    ):
        if not isinstance(tenant_id, str) or not tenant_id:
            raise EvaError(Code.TENANT_REQUIRED, "模型调用必须带租户")
        self.tenant_id, self.cfg = tenant_id, config
        self._breakers = breakers or _process_breakers(config.breaker_failures, config.breaker_probe_seconds)
        self._open_ledger = open_ledger_fn or (lambda: open_ledger(tenant_id))
        self._client_factory = client_factory or self._sdk_client
        self._gate = gate or _process_gate(config.max_in_flight)
        self._sleep, self._jitter, self._now = sleep, jitter, now
        self._recorder = recorder  # (会话, 记录名, 记录, 哈希到大文本)；写失败直接抛，由装配方决定开不开
        self._clients: Dict[str, object] = {}

    @property
    def context_window(self) -> int:
        """主备中较小的窗口：备选随时可能顶上，压缩线按它算才不会在切换后溢出。"""
        return min(self.cfg.models[n].context_window for n in (self.cfg.primary, self.cfg.fallback))

    def call(self, session_id: str, purpose: str, messages: list, tools: Optional[list] = None,
             max_output_tokens: Optional[int] = None, only: Optional[str] = None,
             thinking: bool = False) -> ModelResult:
        """先主选后备选。致命错误（鉴权、溢出、被拒、预算、存储）直接抛；可用性失败用尽后抛 E5002 / E5003。
        only 指定模型名时只用它，不切备选（看图这类只有某个模型做得了的调用）。
        thinking 为真时叠上该模型的 thinking_extra_body；模型没有配置就和不要求一样。"""
        if only is not None and only not in self.cfg.models:
            raise EvaError(Code.MODEL_CONFIG_INVALID, "指定的模型不在配置里", model=only)
        call_id, state, failures = ids.new_id(), {"attempts": 0}, []
        request = (session_id, purpose, messages, tools, max_output_tokens or self.cfg.max_output_tokens, thinking)
        for name in (only,) if only else (self.cfg.primary, self.cfg.fallback):
            spec = self.cfg.models[name]
            try:
                return self._run_model(spec, call_id, state, request)
            except _Failure as f:
                failures.append({"model": name, "kind": f.kind, "detail": f.detail})
                if name == self.cfg.primary:
                    logs.emit(log, logging.WARNING, "主选模型不可用，切备选", model=name, kind=f.kind,
                              fallback=self.cfg.fallback)
        quota_only = all(f["kind"] == "quota" for f in failures)
        raise EvaError(Code.MODEL_QUOTA_EXHAUSTED if quota_only else Code.MODEL_UNAVAILABLE,
                       "主备模型都不可用", failures=failures)

    def _run_model(self, spec, call_id, state, request) -> ModelResult:
        breaker = self._breakers.get(spec.name)
        last = None
        for retry in range(self.cfg.max_retries + 1):
            if not breaker.allow():
                logs.emit(log, logging.WARNING, "熔断开启，跳过该模型", model=spec.name)
                raise last or _Failure("breaker_open", "熔断开启")
            state["attempts"] += 1
            started = time.monotonic()
            try:
                result = self._attempt(spec, f"{call_id}:{state['attempts']}", request)
            except _Failure as f:
                if f.kind == "quota":
                    breaker.success()  # 服务可达，只是额度没了；切备选
                    logs.emit(log, logging.WARNING, "模型额度耗尽", model=spec.name, detail=f.detail)
                    raise
                breaker.failure()
                last = f
                will_retry = retry < self.cfg.max_retries
                logs.emit(log, logging.WARNING, "模型调用失败", model=spec.name, kind=f.kind, detail=f.detail,
                          attempt=state["attempts"], seconds=round(time.monotonic() - started, 1), retrying=will_retry)
                if will_retry:
                    self._sleep(self._delay(retry, f.retry_after))
                continue
            except EvaError as e:
                if e.code is not Code.MODEL_BUSY:  # 排队超时没碰过服务，不能当作服务可达
                    breaker.success()  # 服务可达，是请求或配置的问题；让探针不至于悬空
                raise
            breaker.success()
            logs.emit(log, logging.INFO, "模型调用完成", model=spec.name, purpose=request[1],
                      attempts=state["attempts"], seconds=round(time.monotonic() - started, 1),
                      tokens_in=result.tokens_in, tokens_out=result.tokens_out, cache_hit=result.cache_hit_tokens,
                      extra_body=_body(spec, request[5]))
            return replace(result, attempts=state["attempts"], seconds=round(time.monotonic() - started, 1))
        raise last

    def _delay(self, retry: int, retry_after: Optional[float]) -> float:
        c = self.cfg
        base = max(c.backoff_base_seconds * 2 ** retry, retry_after or 0)
        return min(base, c.max_delay_seconds) + self._jitter(0, c.backoff_jitter_seconds)

    def _attempt(self, spec: ModelSpec, idem_key: str, request) -> ModelResult:
        session_id, purpose, messages, tools, max_out, thinking = request
        client = self._client(spec)
        at = self._now()  # 预扣和结算共用请求发出时刻，分时价不会因跨时段而少扣
        with self._open_ledger() as led:
            hold_id = led.hold(session_id, idem_key, purpose, self._estimate(spec, messages, tools, max_out, at),
                               self.cfg.pricing_version, model=spec.name, budget_cny=self.cfg.budget_per_day_cny)
        try:
            kwargs = {"model": spec.name, "messages": messages, "max_tokens": max_out}
            if tools:
                kwargs["tools"] = tools
            body = _body(spec, thinking)
            if body:
                kwargs["extra_body"] = body
            if not self._gate.acquire(timeout=self.cfg.queue_timeout_seconds):
                raise EvaError(Code.MODEL_BUSY, "模型请求排队超时", model=spec.name,
                               max_in_flight=self.cfg.max_in_flight, waited_seconds=self.cfg.queue_timeout_seconds)
            try:
                resp = client.chat.completions.create(**kwargs)
            except Exception as e:
                failure = classify(e, spec)
                if failure is None:
                    raise
                raise failure from e
            finally:
                self._gate.release()
            result = self._parse(spec, resp, at)
        except BaseException as e:
            with self._open_ledger() as led:
                led.release(hold_id, idem_key)
            if isinstance(e, Exception):
                self._record(spec, idem_key, request, error=_error_info(e))
            raise
        with self._open_ledger() as led:
            led.settle(hold_id, idem_key, result.cost_cny, self.cfg.pricing_version, model=spec.name,
                       tokens_in=result.tokens_in, tokens_out=result.tokens_out,
                       cache_hit_tokens=result.cache_hit_tokens,
                       cache_miss_tokens=result.tokens_in - result.cache_hit_tokens)
        # 先结算再记录：钱已经花了，账要如实；记录写失败则这次调用抛错，不返回一个没留底的结果
        self._record(spec, idem_key, request, reply={
            "content": result.content, "tool_calls": result.tool_calls, "finish_reason": result.finish_reason,
            "reasoning": getattr(resp.choices[0].message, "reasoning_content", None),
            "tokens_in": result.tokens_in, "tokens_out": result.tokens_out, "cost_cny": str(result.cost_cny)})
        return result

    def _record(self, spec: ModelSpec, idem_key: str, request, reply: Optional[dict] = None,
                error: Optional[dict] = None) -> None:
        """写调用记录。turn 的消息体不存：它由 transcript 重算；其余用途（工具内调用、压缩）没有别处可查，全存。"""
        if self._recorder is None:
            return
        session_id, purpose, messages, tools, max_out, thinking = request
        system = next((m["content"] for m in messages if m.get("role") == "system" and isinstance(m.get("content"), str)), None)
        tools_text = json.dumps(tools, ensure_ascii=False, sort_keys=True) if tools else None
        blobs = {}

        def sha_of(text: Optional[str]) -> Optional[str]:
            if text is None:
                return None
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            blobs[sha] = text
            return sha

        record = {"idem_key": idem_key, "purpose": purpose, "model": spec.name, "thinking": thinking,
                  "max_output_tokens": max_out, "code_version": code_version(),
                  "system_sha": sha_of(system), "tools_sha": sha_of(tools_text),
                  "messages": None if purpose == Purpose.TURN.value else _without_images(messages)[0],
                  "reply": reply, "error": error}
        self._recorder(session_id, idem_key, record, blobs)

    def _parse(self, spec: ModelSpec, resp, at: datetime):
        """usage 缺失或自相矛盾的应答当作瞬时失败：记不了准确的账，不能当成功往下传。"""
        try:
            choice, usage = resp.choices[0], resp.usage.model_dump()
            tokens_in, tokens_out = int(usage["prompt_tokens"]), int(usage["completion_tokens"])
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            raise _Failure("transient", "应答缺少 choices 或 usage") from None
        hit = _dig(usage, spec.cache_hit_path) or 0
        if not isinstance(hit, int) or not 0 <= hit <= tokens_in or tokens_in < 0 or tokens_out < 0:
            raise _Failure("transient", "usage 数字自相矛盾")
        p = spec.prices_per_million(tokens_in, at)
        miss = tokens_in - hit
        cost = ((miss * p["input_cache_miss"] + hit * p["input_cache_hit"] + tokens_out * p["output"]) / _MILLION
                ).quantize(_CENT, ROUND_HALF_UP)
        calls = [{"id": t.id, "name": t.function.name, "arguments": t.function.arguments}
                 for t in (choice.message.tool_calls or [])]
        reasoning = getattr(choice.message, "reasoning_content", None)
        kept = reasoning[:self.cfg.reasoning_max_chars] if isinstance(reasoning, str) and reasoning else None
        return ModelResult(choice.message.content, calls, choice.finish_reason, spec.name,
                           tokens_in, tokens_out, hit, cost, 0, kept or None, len(reasoning) if kept else 0)

    def _estimate(self, spec: ModelSpec, messages, tools, max_out: int, at: datetime) -> Decimal:
        """预扣上限。没有分词器，输入按一字符一 token 取上界，全按未命中缓存的价格；
        档也按字符数选，档价随输入单调不降，所以不会低于真实所在档。图片的 base64 不按字符算，一张按 image_token_bound。"""
        text, images = _without_images(messages)
        chars = len(json.dumps([text, tools], ensure_ascii=False, default=str)) + images * self.cfg.image_token_bound
        p = spec.prices_per_million(chars, at)
        return ((chars * p["input_cache_miss"] + max_out * p["output"]) / _MILLION).quantize(_CENT, ROUND_UP)

    def _client(self, spec: ModelSpec):
        if spec.name not in self._clients:
            self._clients[spec.name] = self._client_factory(spec)
        return self._clients[spec.name]

    def _sdk_client(self, spec: ModelSpec):
        key, url = cfg_mod.credentials(spec)
        return openai.OpenAI(api_key=key, base_url=url, timeout=self.cfg.attempt_seconds, max_retries=0)


def _error_info(e: Exception) -> dict:
    if isinstance(e, _Failure):
        return {"kind": e.kind, "detail": e.detail}
    if isinstance(e, EvaError):
        return e.to_dict()
    return {"type": type(e).__name__, "detail": str(e)}


def _without_images(messages: list):
    """把消息里 image_url 的内容换成空串，返回（换后的消息, 图片张数）。不改原消息。"""
    count, out = 0, []
    for m in messages:
        parts = m.get("content") if isinstance(m, dict) else None
        if not isinstance(parts, list):
            out.append(m)
            continue
        new = []
        for p in parts:
            if isinstance(p, dict) and p.get("type") == "image_url":
                count += 1
                p = {"type": "image_url", "image_url": {"url": ""}}
            new.append(p)
        out.append({**m, "content": new})
    return out, count


@functools.lru_cache(maxsize=None)
def _process_gate(size: int) -> threading.BoundedSemaphore:
    """进程级：所有租户的模型请求共用一个并发闸。"""
    return threading.BoundedSemaphore(size)


@functools.lru_cache(maxsize=None)
def _process_breakers(failures: int, probe_after_seconds: float) -> Breakers:
    """进程级：相同参数全进程共用一组熔断器。"""
    return Breakers(failures, probe_after_seconds)


