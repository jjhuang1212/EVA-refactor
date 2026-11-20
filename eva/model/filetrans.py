"""长录音转写：异步接口，上传 → 提交 → 轮询 → 下载结果，可带说话人分离。

和同步转写（asr.py）走同一套约束：每一段先在账本预扣、按结果里的 usage 结算、失败释放；全进程共用并发闸和熔断；
密钥只读环境变量。厂商的应答形状只在这两个文件里认识。
任务被受理之后（提交成功）才可能计费：之前的瞬时故障原地重试；之后轮询超时不自动重投，task_id 写进错误详情和日志供人工核对，
免得重投再付一次钱。
"""
import logging
import threading
import time
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Callable, Optional, Tuple

import requests

from eva.common import ids, logs
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import Purpose
from eva.model import asr
from eva.model.asr import AsrConfig, Sentence, Transcription
from eva.model.breaker import Breakers
from eva.storage.ledger import open_ledger

log = logging.getLogger(__name__)
_CENT = Decimal("0.0001")
_MILLION = Decimal(1_000_000)
_POLL_NET_RETRIES = 3  # 轮询是幂等的 GET，网络抖动多试几次不会重复计费
_FAILED = ("FAILED", "CANCELED", "UNKNOWN")
# 任务 FAILED 时厂商 code 的族名：输入本身不合规，重投一万次也一样；认不准的一律按服务故障（可重试）
_INPUT_REJECTED = ("InvalidParameter", "InvalidFile", "IllegalFile", "UnsupportedFile", "FileFormat", "DurationExceeded",
                   "BadRequest", "DataInspection")


class _Transient(Exception):
    """可用性类失败，任务还没被受理（或被厂商判为服务故障），可以原地重试。"""

    def __init__(self, detail: str, retry_after: Optional[float] = None):
        super().__init__(detail)
        self.detail, self.retry_after = detail, retry_after


def _speaker(raw) -> str:
    return f"S{raw + 1}" if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else ""


def sentences_from(payload: dict) -> Tuple[Sentence, ...]:
    """结果 JSON → 句子。句子缺时间锚点或结构不对抛 ValueError，由调用方当作应答损坏。"""
    out = []
    for transcript in payload["transcripts"]:
        for s in transcript["sentences"]:
            text = (s.get("text") or "").strip()
            if not text:
                continue
            begin, end = s["begin_time"], s["end_time"]
            if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (begin, end)) or end < begin:
                raise ValueError(f"句子时间不合法 {begin}-{end}")
            out.append(Sentence(begin, end, text, _speaker(s.get("speaker_id"))))
    out.sort(key=lambda x: (x.begin_ms, x.end_ms))
    return tuple(out)


class Filetrans:
    def __init__(self, tenant_id: str, cfg: AsrConfig, budget_cny, http=None, open_ledger_fn: Callable = open_ledger,
                 breakers: Optional[Breakers] = None, gate: Optional[threading.BoundedSemaphore] = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        if not isinstance(tenant_id, str) or not tenant_id:
            raise EvaError(Code.TENANT_REQUIRED, "转写必须带租户")
        self.tenant_id, self.cfg, self._lc, self._budget = tenant_id, cfg, cfg.long, budget_cny
        self._http, self._open_ledger, self._sleep, self._clock = http or requests.Session(), open_ledger_fn, sleep, clock
        self._breaker = (breakers or asr._process_breakers(cfg.breaker_failures, cfg.breaker_probe_seconds)).get(self._lc.model)
        self._gate = gate or asr._process_gate(cfg.max_in_flight)

    def transcribe(self, session_id: str, wav: bytes, seconds: float, cancel: threading.Event,
                   speaker_count: Optional[int] = None) -> Transcription:
        """wav 是一段不超过 part_max_seconds 的标准 wav。speaker_count 是调用方已知的说话人数，不传由厂商自己判。
        故障抛 E5xxx；没有语音内容返回空句子列表。"""
        if not wav or seconds <= 0:
            raise EvaError(Code.INVALID_VALUE, "音频为空或时长不合法", seconds=seconds)
        call_id, last = ids.new_id(), None
        for attempt in range(self.cfg.max_retries + 1):
            if not self._breaker.allow():
                raise last or EvaError(Code.MODEL_UNAVAILABLE, "转写熔断开启，暂不请求", model=self._lc.model)
            started = self._clock()
            try:
                result = self._attempt(session_id, f"{call_id}:{attempt + 1}", wav, seconds, cancel, speaker_count)
            except _Transient as t:
                self._breaker.failure()
                logs.emit(log, logging.WARNING, "长录音转写失败", model=self._lc.model, detail=t.detail, attempt=attempt + 1,
                          seconds=round(self._clock() - started, 1))
                last = EvaError(Code.MODEL_UNAVAILABLE, "转写服务不可用", model=self._lc.model, reason=t.detail, attempts=attempt + 1)
                if attempt < self.cfg.max_retries:
                    self._sleep(min(max(self.cfg.backoff_base_seconds * 2 ** attempt, t.retry_after or 0), self.cfg.max_delay_seconds))
                continue
            except EvaError as e:
                if e.code is not Code.MODEL_BUSY and e.code is not Code.TOOL_ABORTED:
                    self._breaker.success()
                raise
            self._breaker.success()
            logs.emit(log, logging.INFO, "长录音转写完成", model=self._lc.model, audio_seconds=round(seconds, 1),
                      sentences=len(result.sentences), tokens_in=result.input_tokens, tokens_out=result.output_tokens,
                      seconds=round(self._clock() - started, 1))
            return result
        raise last

    def _attempt(self, session_id: str, idem: str, wav: bytes, seconds: float, cancel, speaker_count) -> Transcription:
        cfg = self.cfg
        key, base = asr.credentials(cfg)
        with self._open_ledger(self.tenant_id) as led:
            hold_id = led.hold(session_id, idem, Purpose.ASR.value, self._estimate(seconds), cfg.pricing_version,
                               model=self._lc.model, budget_cny=self._budget)
        try:
            payload = self._run(key, base.rstrip("/"), wav, f"{idem.replace(':', '-')}.wav", cancel, speaker_count)
            result = self._result(payload, seconds)
        except BaseException:
            with self._open_ledger(self.tenant_id) as led:
                led.release(hold_id, idem)
            raise
        with self._open_ledger(self.tenant_id) as led:
            led.settle(hold_id, idem, result.cost_cny, cfg.pricing_version, model=self._lc.model,
                       tokens_in=result.input_tokens, tokens_out=result.output_tokens, asr_seconds=result.seconds)
        return result

    def _estimate(self, seconds: float) -> Decimal:
        c = self.cfg
        return ((Decimal(seconds) * c.estimate_input_tps * c.price_input + Decimal(seconds) * c.estimate_output_tps * c.price_output)
                / _MILLION).quantize(_CENT, ROUND_UP)

    def _result(self, payload: dict, seconds: float) -> Transcription:
        c = self.cfg
        try:
            usage = payload["usage"]
            tokens_in, tokens_out = int(usage["input_tokens"]), int(usage["output_tokens"])
            if tokens_in < 0 or tokens_out < 0:
                raise ValueError("token 数为负")
            sentences = sentences_from(payload)
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise EvaError(Code.MODEL_UNAVAILABLE, "转写结果缺少 usage 或句子时间，或内容自相矛盾（任务已成功，费用可能已产生，不要重投）",
                           model=self._lc.model, reason="malformed_result", error=str(e)) from e
        cost = ((tokens_in * c.price_input + tokens_out * c.price_output) / _MILLION).quantize(_CENT, ROUND_HALF_UP)
        return Transcription(sentences, round(seconds), tokens_in, tokens_out, cost)

    # ---------- 厂商的四步 ----------

    def _run(self, key: str, base: str, wav: bytes, name: str, cancel, speaker_count) -> dict:
        headers = {"Authorization": f"Bearer {key}"}
        if not self._gate.acquire(timeout=self.cfg.queue_timeout_seconds):  # 闸只管上传和提交；轮询是廉价的 GET，不占额度
            raise EvaError(Code.MODEL_BUSY, "转写请求排队超时", model=self._lc.model, max_in_flight=self.cfg.max_in_flight,
                           waited_seconds=self.cfg.queue_timeout_seconds)
        try:
            oss_url = self._upload(base, headers, wav, name)
            task_id = self._submit(base, headers, oss_url, speaker_count)
        finally:
            self._gate.release()
        output = self._wait(base, headers, task_id, cancel)
        return self._download(output, task_id)

    def _call(self, fn, what: str):
        try:
            return fn()
        except requests.ReadTimeout as e:
            raise _Transient(f"{what} 读超时") from e
        except requests.RequestException as e:
            raise _Transient(f"{what} 连接失败 {type(e).__name__}") from e

    def _check(self, resp, what: str) -> dict:
        """非 200 的分流：额度、鉴权、拒绝不重试；429、5xx 原地重试。"""
        c, status = self.cfg, resp.status_code
        if status == 200:
            return resp
        code, message = asr._provider_error(resp)
        if status in c.quota_statuses or any(code.startswith(p) for p in c.quota_code_prefixes):
            raise EvaError(Code.MODEL_QUOTA_EXHAUSTED, "转写额度耗尽", model=self._lc.model, status=status, error_code=code)
        if status in (401, 403):
            raise EvaError(Code.MODEL_AUTH, "转写鉴权失败", model=self._lc.model, status=status, error_code=code)
        if status == 429 or status >= 500 or status == 408:
            header = getattr(resp, "headers", None) and resp.headers.get("retry-after")
            try:
                retry_after = float(header) if header else None
            except ValueError:
                retry_after = None
            raise _Transient(f"{what} {status} {code}", retry_after)
        raise EvaError(Code.MODEL_REQUEST_REJECTED, "转写请求被厂商拒绝", model=self._lc.model, status=status, error_code=code,
                       reason=message, step=what)

    def _json(self, resp, what: str) -> dict:
        try:
            body = resp.json()
        except ValueError as e:
            raise _Transient(f"{what} 应答不是 JSON") from e
        if not isinstance(body, dict):
            raise _Transient(f"{what} 应答不是 JSON 对象")
        return body

    def _upload(self, base: str, headers: dict, wav: bytes, name: str) -> str:
        lc = self._lc
        resp = self._check(self._call(lambda: self._http.get(f"{base}/uploads", params={"model": lc.model, "action": "getPolicy"},
                                                             headers=headers, timeout=60), "取上传凭证"), "取上传凭证")
        policy = self._json(resp, "取上传凭证").get("data")
        needed = ("upload_dir", "oss_access_key_id", "signature", "policy", "x_oss_object_acl", "x_oss_forbid_overwrite", "upload_host")
        if not isinstance(policy, dict) or any(not policy.get(k) for k in needed):
            raise _Transient("上传凭证缺项")
        key = f"{policy['upload_dir']}/{name}"
        form = {"OSSAccessKeyId": policy["oss_access_key_id"], "Signature": policy["signature"], "policy": policy["policy"],
                "key": key, "x-oss-object-acl": policy["x_oss_object_acl"],
                "x-oss-forbid-overwrite": policy["x_oss_forbid_overwrite"], "success_action_status": "200",
                "x-oss-content-type": "audio/wav"}
        up = self._call(lambda: self._http.post(policy["upload_host"], data=form, files={"file": (name, wav)},
                                                timeout=(self.cfg.connect_seconds, lc.upload_timeout_seconds)), "上传")
        if up.status_code != 200:
            raise _Transient(f"上传 {up.status_code}")
        return f"oss://{key}"

    def _submit(self, base: str, headers: dict, oss_url: str, speaker_count) -> str:
        lc = self._lc
        params = {"channel_id": [0], "enable_words": False}
        if lc.diarization:
            params["diarization_enabled"] = True
            if speaker_count is not None:
                params["speaker_count"] = speaker_count
        body = {"model": lc.model, "input": {"file_urls": [oss_url]}, "parameters": params}
        # 没有 OssResourceResolve 头时 oss:// 任务稳定报 SERVER_ERROR
        hdr = {**headers, "Content-Type": "application/json", "X-DashScope-Async": "enable", "X-DashScope-OssResourceResolve": "enable"}
        resp = self._check(self._call(lambda: self._http.post(f"{base}/services/audio/asr/transcription", headers=hdr, json=body,
                                                              timeout=(self.cfg.connect_seconds, 60)), "提交任务"), "提交任务")
        task_id = (self._json(resp, "提交任务").get("output") or {}).get("task_id")
        if not task_id:
            raise _Transient("提交任务没有返回 task_id")
        return task_id

    def _wait(self, base: str, headers: dict, task_id: str, cancel) -> dict:
        lc = self._lc
        deadline = self._clock() + lc.poll_max_wait_seconds
        while True:
            if cancel.is_set():
                raise EvaError(Code.TOOL_ABORTED, "转写等待被取消（任务已提交，可能已计费）", task_id=task_id)
            output = self._poll_once(base, headers, task_id)
            status = output.get("task_status")
            if status == "SUCCEEDED":
                return output
            if status in _FAILED:
                vendor = str(output.get("code") or "")
                if vendor.startswith(_INPUT_REJECTED):
                    raise EvaError(Code.MODEL_REQUEST_REJECTED, "录音被厂商判为不合规，重投不会变好", model=lc.model, task_id=task_id,
                                   error_code=vendor, reason=str(output.get("message") or "")[:200])
                raise _Transient(f"任务失败 {status} {vendor} task_id={task_id}")
            if self._clock() > deadline:
                raise EvaError(Code.MODEL_UNAVAILABLE, "转写轮询超时，任务已受理、可能已计费，不要直接重投", model=lc.model,
                               task_id=task_id, reason="poll_timeout", waited_seconds=lc.poll_max_wait_seconds)
            self._sleep(lc.poll_interval_seconds)

    def _poll_once(self, base: str, headers: dict, task_id: str) -> dict:
        for n in range(1, _POLL_NET_RETRIES + 1):
            try:
                resp = self._check(self._call(lambda: self._http.get(f"{base}/tasks/{task_id}", headers=headers, timeout=60),
                                              "查询任务"), "查询任务")
                return self._json(resp, "查询任务").get("output") or {}
            except _Transient as t:
                if n == _POLL_NET_RETRIES:
                    raise EvaError(Code.MODEL_UNAVAILABLE, "查询转写任务失败，任务已受理、可能已计费，不要直接重投", model=self._lc.model,
                                   task_id=task_id, reason=t.detail) from t
                self._sleep(self._lc.poll_interval_seconds)

    def _download(self, output: dict, task_id: str) -> dict:
        url = output.get("transcription_url") or next((r.get("transcription_url") for r in output.get("results") or []
                                                       if isinstance(r, dict) and r.get("transcription_url")), None)
        gone = EvaError(Code.MODEL_UNAVAILABLE, "转写任务已成功但取不到结果（费用已产生，不要直接重投）", model=self._lc.model,
                        task_id=task_id, reason="result_unavailable")
        if not url:
            raise gone
        try:
            resp = self._http.get(url, timeout=120)  # 带签名的临时地址，不带鉴权头
            payload = resp.json() if resp.status_code == 200 else None
        except (requests.RequestException, ValueError) as e:
            raise gone from e
        if not isinstance(payload, dict):
            raise gone
        return payload
