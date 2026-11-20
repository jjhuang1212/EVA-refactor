"""把租户的文件解析成块：先查解析缓存，没有就送进解析沙箱，结果写回缓存。
沙箱里要看图的图（内嵌图，或 OCR 没读出文字的图）只留占位块，图存进对象存储、sha256 记在解析结果里，解析结果带占位块照常缓存；
每次读取时宿主再问视觉模型（eva/tools/vision.py）把描述补进去，看成的有图级缓存，没看成的下次再试，不重跑沙箱。

解析器代码本身随每次运行放进沙箱的工作目录，宿主进程不 import 解析库。缓存键 = 租户内容哈希 + 解析器版本，
版本是解析器源码与解析上限的哈希，改了代码或上限就自动换新，不靠人记得升版本号。
"""
import dataclasses
import hashlib
import json
import logging
import time
from collections import Counter
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import eva
from eva.common import ids, logs
from eva.contracts.document import AudioLimits, Parsed
from eva.contracts.errors import Code, EvaError
from eva.model.client import ModelClient
from eva.parsing import SUPPORTED
from eva.parsing.run import AUDIO_OUT, VISION_DIR
from eva.sandbox import Sandbox
from eva.storage.objects import ObjectStore, Objects
from eva.tools.config import ParsingConfig
from eva.tools.vision import Vision

log = logging.getLogger(__name__)
_RUN_CODE = "from eva.parsing.run import main\nmain()\n"
_SHARED = ("__init__.py", "contracts/__init__.py", "contracts/errors.py", "contracts/vocab.py", "contracts/document.py")


def _sources() -> Dict[str, bytes]:
    root = Path(eva.__file__).resolve().parent
    paths = [root / name for name in _SHARED] + sorted((root / "parsing").glob("*.py"))
    return {f"eva/{p.relative_to(root).as_posix()}": p.read_bytes() for p in paths}


class Documents:
    supported = SUPPORTED

    def __init__(self, box: Sandbox, cfg: ParsingConfig, vision: Optional[Vision] = None):
        self._box, self.cfg, self.vision, self._sources = box, cfg, vision, _sources()
        # 没有视觉模型时不留占位块，OCR 读不出的图和以前一样记警告
        self._limits = cfg.limits if vision else dataclasses.replace(cfg.limits, vision_max_images=0)
        digest = hashlib.sha256(json.dumps(dataclasses.asdict(self._limits), sort_keys=True).encode())
        digest.update(vision.version.encode() if vision else b"-")
        for name, data in sorted(self._sources.items()):
            digest.update(name.encode() + b"\0" + data)
        self.version = digest.hexdigest()[:12]

    def _run(self, tenant_id: str, filename: str, data: bytes, extra: dict, cancel, want_audio: bool = False):
        """在一次性的沙箱里跑一个任务，返回 (parsed.json 的内容, 标准 wav 或 None, 留给视觉模型的图)。任务自己报告的错误原样抛出。"""
        session = f"parse-{ids.new_id()}"
        suffix = Path(filename).suffix.lower()
        job = {"filename": filename, "input": f"in/source{suffix}", "version": self.version,
               "limits": dataclasses.asdict(self._limits), **extra}
        files = {**self._sources, "job.json": json.dumps(job).encode("utf-8"), job["input"]: data}
        wav, images = None, {}
        try:
            res = self._box.run(tenant_id, session, _RUN_CODE, files, cancel)
            produced = {a.path for a in res.files}
            if res.exit_code != 0 or "parsed.json" not in produced:
                raise EvaError(Code.TOOL_FAILED, "解析进程异常退出", exit_code=res.exit_code, stderr=res.stderr[-500:])
            result = json.loads(self._box.read_artifact(tenant_id, session, "parsed.json"))
            if want_audio and "audio" in result and not result["audio"]["silent"]:
                try:
                    wav = self._box.read_artifact(tenant_id, session, AUDIO_OUT)
                except EvaError as e:
                    if e.code is not Code.SANDBOX_ARTIFACT_REJECTED:
                        raise
                    # 转出的 wav 被沙箱拒收是配置问题，不是用户文件的问题；具体原因进日志，模型只拿到不含数字的说明
                    logs.report_error(log, e, file=filename, stage="audio_wav")
                    raise EvaError(Code.TOOL_FAILED, "录音转码没有成功，是系统问题，和用户的文件无关", file=filename) from e
            for ord_ in result.get("vision", ()):
                images[ord_] = self._box.read_artifact(tenant_id, session, f"{VISION_DIR}/{ord_}.png")
        finally:
            self._box.discard(tenant_id, session)
        if "error" in result:
            err = result["error"]
            raise EvaError(Code(err["code"]), err["message"], **err["details"])
        return result, wav, images

    def parse(self, tenant_id: str, filename: str, data: bytes, cancel) -> Tuple[Parsed, Dict[int, bytes]]:
        """在一次性的解析沙箱里解析，返回（解析结果, 留给视觉模型的图：占位块的块序号 → png）。
        文件自身的问题（损坏、加密、不支持）原样抛 E2002；解析进程异常退出抛 E6001。"""
        result, _, images = self._run(tenant_id, filename, data, {}, cancel)
        return Parsed.from_dict(result["parsed"]), images

    def prepare_audio(self, tenant_id: str, filename: str, data: bytes, limits: AudioLimits,
                      cancel) -> Tuple[Optional[bytes], dict]:
        """把录音转成转写用的标准 wav。返回 (wav 或 None, 信息)；静音时 wav 为 None，信息里 silent=True。
        解不开、太长抛 E2002（reason 为 corrupt、too_long）。"""
        result, wav, _ = self._run(tenant_id, filename, data, {"audio": dataclasses.asdict(limits)}, cancel, want_audio=True)
        return wav, result["audio"]


def _seen(documents: Documents, store: ObjectStore, open_tx, tenant_id: str, session_id: str, parsed: Parsed,
          model: Optional[ModelClient]) -> Parsed:
    """有占位块就让视觉模型补上描述；缓存里的解析结果永远是带占位块的那一份。"""
    if not parsed.images:
        return parsed
    return documents.vision.fill(model, store, open_tx, tenant_id, session_id, parsed, documents.cfg.limits.block_max_chars)


def load_parsed(documents: Documents, store: ObjectStore, open_tx: Callable[[], AbstractContextManager],
                tenant_id: str, session_id: str, file_id: str, cancel, model: Optional[ModelClient] = None) -> Tuple[dict, Parsed]:
    """返回（登记行, 解析结果）。事务只包存储读写，解析沙箱和看图运行期间不占着数据库连接。"""
    with open_tx() as conn:
        objects = Objects(conn, tenant_id, store)
        row = objects.describe(file_id)
        cached = objects.read_parsed(file_id, documents.version)
        if cached is None:
            _, data = objects.read_original(file_id, documents.cfg.max_file_bytes)
    if cached is not None:
        parsed = Parsed.from_dict(json.loads(cached))
        logs.emit(log, logging.INFO, "解析缓存命中", file=row["filename"], format=parsed.format, blocks=len(parsed.blocks))
        return row, _seen(documents, store, open_tx, tenant_id, session_id, parsed, model)
    started = time.monotonic()
    try:
        parsed, images = documents.parse(tenant_id, row["filename"], data, cancel)
    except EvaError as e:
        logs.emit(log, logging.WARNING, "文件解析失败", file=row["filename"], bytes=len(data), code=e.code.value,
                  reason=e.details.get("reason"), seconds=round(time.monotonic() - started, 1))
        raise
    if images:
        shas = {ord_: hashlib.sha256(png).hexdigest() for ord_, png in images.items()}
        parsed = dataclasses.replace(parsed, images=shas)
        with open_tx() as conn:
            objects = Objects(conn, tenant_id, store)
            for ord_, png in images.items():
                objects.put_vision_image(shas[ord_], png)
    logs.emit(log, logging.INFO, "文件解析完成", file=row["filename"], format=parsed.format, bytes=len(data),
              blocks=len(parsed.blocks), images=len(images), warnings=dict(Counter(w.get("reason") for w in parsed.warnings)),
              seconds=round(time.monotonic() - started, 1))
    with open_tx() as conn:
        Objects(conn, tenant_id, store).put_parsed(file_id, documents.version,
                                                    json.dumps(parsed.to_dict(), ensure_ascii=False).encode("utf-8"))
    return row, _seen(documents, store, open_tx, tenant_id, session_id, parsed, model)
