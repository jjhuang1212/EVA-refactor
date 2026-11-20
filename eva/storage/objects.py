"""对象存储（MinIO）。MinIO 客户端只在这里；对象键由 tenant_id 拼出，业务代码不拼。

存储本身不做访问控制，租户隔离靠这里强制加前缀：按键读取前先确认键在本租户前缀之下，
别的租户或别的会话的键一律当作"查不到"。
"""
import hashlib
import io
import json
import os
import re
from pathlib import Path
from typing import Dict, Optional

import minio
import urllib3
import yaml
from dotenv import load_dotenv
from minio.commonconfig import ENABLED, Filter
from minio.error import S3Error
from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule

from eva.common import ids
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import FileStatus, TenantRealm
from eva.storage import tenants
from eva.storage.files import Files
from eva.storage.repo import TenantRepo

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "configs" / "storage.yaml"
_NOT_FOUND = ("NoSuchKey", "NoSuchObject")
_VERSION = re.compile(r"[0-9a-f]{12}")
_AREA = re.compile(r"(cache|diag)/[a-z_]+")
_RULE_ID = "eva-expire-"  # 本程序管的过期规则的 ID 前缀，同步规则时只动这一类
# （较长的, 较短的）：后者的内容引用前者，前者先过期会让后者读不到
_LONGER = (("cache/vision", "cache/parsed"), ("diag/blobs", "diag/calls"))
_KEY_PART = re.compile(r"[A-Za-z0-9_:\-]{1,80}")


def _key_part(value) -> str:
    """拼进对象键的会话 ID、记录名只允许这个字符集，不让 ../ 之类逃出本租户前缀。"""
    if not isinstance(value, str) or not _KEY_PART.fullmatch(value):
        raise EvaError(Code.FILE_NOT_FOUND, "对象不存在", name=str(value)[:80])
    return value


class ObjectStore:
    """桶内按完整对象键读写字节。不认识租户，租户前缀由 Objects 负责。"""

    def __init__(self, bucket: str, client):
        self.bucket, self._client = bucket, client

    @classmethod
    def from_config(cls, path: Path = DEFAULT_PATH) -> "ObjectStore":
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了存储配置", path=str(path), error=str(e)) from e
        if not isinstance(raw, dict) or not isinstance(raw.get("bucket"), str) or not raw["bucket"] \
                or not isinstance(raw.get("secure"), bool):
            raise EvaError(Code.CONFIG_INVALID, "存储配置需要字符串 bucket 和布尔 secure", path=str(path))
        load_dotenv()
        names = ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY")
        missing = [n for n in names if not os.environ.get(n)]
        if missing:
            raise EvaError(Code.STORAGE_ERROR, "缺少对象存储环境变量", missing=missing)
        client = minio.Minio(os.environ[names[0]], access_key=os.environ[names[1]],
                             secret_key=os.environ[names[2]], secure=raw["secure"])
        return cls(raw["bucket"], client)

    def apply_lifecycle(self, expire_days: Dict[str, int]) -> None:
        """让桶的过期规则和配置一致。本程序管的规则（ID 以 eva-expire- 开头）整体换成配置里的，别的规则原样保留。
        规则按对象创建（最后写入）时间算天数，所以按内容哈希重复写入的对象每次写都会续命。"""
        try:
            try:
                current = self._client.get_bucket_lifecycle(self.bucket)
            except S3Error as e:
                if e.code != "NoSuchLifecycleConfiguration":
                    raise
                current = None
            keep = [r for r in (current.rules if current else []) if not (r.rule_id or "").startswith(_RULE_ID)]
            ours = [Rule(ENABLED, rule_filter=Filter(prefix=f"{area}/"), rule_id=_RULE_ID + area.replace("/", "-"),
                         expiration=Expiration(days=days)) for area, days in sorted(expire_days.items())]
            self._client.set_bucket_lifecycle(self.bucket, LifecycleConfig(keep + ours))
        except Exception as e:  # noqa: BLE001 SDK 边界：全部翻译成错误码
            raise _translate(e, "<lifecycle>") from e

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        try:
            self._client.put_object(self.bucket, key, io.BytesIO(data), len(data), content_type=content_type)
        except Exception as e:  # noqa: BLE001 SDK 边界：全部翻译成错误码
            raise _translate(e, key) from e

    def get(self, key: str) -> bytes:
        try:
            resp = self._client.get_object(self.bucket, key)
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()
        except Exception as e:  # noqa: BLE001
            raise _translate(e, key) from e


def load_expire_days(path: Path = DEFAULT_PATH) -> Dict[str, int]:
    """configs/storage.yaml 的 expire_days：对象键的类别前缀（如 cache/parsed）到过期天数。"""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了存储配置", path=str(path), error=str(e)) from e
    days = raw.get("expire_days") if isinstance(raw, dict) else None
    if not isinstance(days, dict) or not days or not all(
            isinstance(k, str) and _AREA.fullmatch(k) and isinstance(v, int) and not isinstance(v, bool) and v > 0
            for k, v in days.items()):
        raise EvaError(Code.CONFIG_INVALID, "expire_days 必须是 类别前缀（cache/xxx 或 diag/xxx）到正整数天数的映射", path=str(path))
    for longer, shorter in _LONGER:
        if longer in days and shorter in days and days[longer] <= days[shorter]:
            raise EvaError(Code.CONFIG_INVALID, f"{longer} 的过期天数必须大于 {shorter}", path=str(path))
    return dict(days)


def _translate(e: Exception, key: str) -> EvaError:
    if isinstance(e, S3Error):
        if e.code in _NOT_FOUND:
            return EvaError(Code.FILE_NOT_FOUND, "对象不存在", key=key)
        return EvaError(Code.STORAGE_ERROR, "对象存储拒绝了请求", key=key, s3_code=e.code, reason=e.message)
    if isinstance(e, (urllib3.exceptions.HTTPError, OSError)):
        return EvaError(Code.STORAGE_UNAVAILABLE, "对象存储连不上或超时", key=key, error=f"{type(e).__name__}: {e}")
    return EvaError(Code.STORAGE_ERROR, "对象存储出现未预期的错误", key=key, error=f"{type(e).__name__}: {e}")


class Objects(TenantRepo):
    def __init__(self, conn, tenant_id: str, store: ObjectStore):
        super().__init__(conn, tenant_id)
        self._store = store

    def _prefix(self) -> str:
        if tenants.get(self.conn, self.tenant_id) is None:
            raise EvaError(Code.TENANT_NOT_FOUND, "租户不存在", tenant_id=self.tenant_id)
        return f"t/{self.tenant_id}/"

    def put_original(self, file_id: str, data: bytes) -> None:
        """写入已登记文件的原件。内容必须和登记的哈希、大小一致。"""
        row = self._file(file_id)
        if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise EvaError(Code.INVALID_VALUE, "内容与登记的哈希或大小不一致", file_id=file_id)
        self._store.put(row["minio_key"], data, row["mime"])

    def save_original(self, filename: str, mime: str, data: bytes):
        """登记并存下一份原件，返回（登记行, 此前是否已有同内容）。同一租户同内容沿用原 file_id。
        登记和写入在调用方的同一个事务里：写对象失败则登记一并回滚，不留下读不出的记录。"""
        files = Files(self.conn, self.tenant_id)
        sha = hashlib.sha256(data).hexdigest()
        existing = files.find_by_sha(sha) is not None
        row = files.register(sha, filename, mime, len(data))
        self.put_original(row["file_id"], data)
        files.set_status(row["file_id"], FileStatus.AVAILABLE.value)
        return row, existing

    def describe(self, file_id: str) -> dict:
        """可用文件的登记行。不存在、不属于本租户、不是可用状态都报 E2001。"""
        row = self._file(file_id)
        if row["status"] != FileStatus.AVAILABLE.value:
            raise EvaError(Code.FILE_NOT_FOUND, "文件不可用", file_id=file_id, status=row["status"])
        return row

    def _under(self, area: str) -> str:
        """会过期的对象放在 <类别>/ 之下，租户前缀放在类别后面：桶级生命周期规则只能按固定前缀过期，新增租户不用改规则。
        类别列在 configs/storage.yaml 的 expire_days 里。"""
        return f"{area}/{self._prefix()}"

    def _parsed_key(self, row: dict, version: str) -> str:
        if not _VERSION.fullmatch(version):
            raise EvaError(Code.INVALID_VALUE, "解析器版本必须是 12 位小写十六进制", value=repr(version))
        return f"{self._under('cache/parsed')}{row['sha256']}/{version}.json"

    def read_parsed(self, file_id: str, version: str) -> Optional[bytes]:
        """该版本解析器对这份内容的解析缓存；没有缓存返回 None，存储本身的故障照常抛错。"""
        key = self._parsed_key(self.describe(file_id), version)
        try:
            return self._store.get(key)
        except EvaError as e:
            if e.code is Code.FILE_NOT_FOUND:
                return None
            raise

    def _vision_key(self, sha: str, version: str) -> str:
        if not _VERSION.fullmatch(version) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise EvaError(Code.INVALID_VALUE, "看图缓存的键必须是 64 位图哈希和 12 位版本", sha=repr(sha), version=repr(version))
        return f"{self._under('cache/vision')}{sha}/{version}.txt"

    def read_vision(self, sha: str, version: str) -> Optional[str]:
        """这张图（按内容哈希）在该视觉版本下的描述；没缓存返回 None，存储本身的故障照常抛错。"""
        try:
            return self._store.get(self._vision_key(sha, version)).decode("utf-8")
        except EvaError as e:
            if e.code is Code.FILE_NOT_FOUND:
                return None
            raise

    def put_vision(self, sha: str, version: str, text: str) -> None:
        self._store.put(self._vision_key(sha, version), text.encode("utf-8"), "text/plain")

    def _image_key(self, sha: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise EvaError(Code.INVALID_VALUE, "解析里的图必须按 64 位内容哈希存取", sha=repr(sha))
        return f"{self._under('cache/vision')}{sha}/image.png"

    def put_vision_image(self, sha: str, png: bytes) -> None:
        """留给视觉模型的图，按内容哈希存，同一张图不论来自哪份文件、哪个解析版本都只有一份。"""
        if hashlib.sha256(png).hexdigest() != sha:
            raise EvaError(Code.INVALID_VALUE, "图的内容与给定的哈希不一致", sha=sha)
        self._store.put(self._image_key(sha), png, "image/png")

    def read_vision_image(self, sha: str) -> bytes:
        """解析结果登记过的图，对象不在就是存储不一致，照常抛 E2001。"""
        return self._store.get(self._image_key(sha))

    def read_latest_parsed(self, file_id: str) -> Optional[bytes]:
        """登记行记录的最近一次解析结果；这份文件还没解析过，或缓存已不在，返回 None。"""
        key = self.describe(file_id)["parsed_key"]
        if not key:
            return None
        try:
            return self._store.get(key)
        except EvaError as e:
            if e.code is Code.FILE_NOT_FOUND:
                return None
            raise

    def point_parsed(self, file_id: str, version: str) -> None:
        """把"最近一次解析"指向已有的某版缓存；已经指着就不动。"""
        row = self.describe(file_id)
        key = self._parsed_key(row, version)
        if row["parsed_key"] != key:
            Files(self.conn, self.tenant_id).set_parsed_key(file_id, key)

    def put_parsed(self, file_id: str, version: str, data: bytes) -> str:
        key = self._parsed_key(self.describe(file_id), version)
        self._store.put(key, data, "application/json")
        Files(self.conn, self.tenant_id).set_parsed_key(file_id, key)
        return key

    def available_named(self, filename: str) -> list:
        """本租户里该文件名的可用文件。同名的可能有多份。"""
        return Files(self.conn, self.tenant_id).available_named(filename)

    def read_original(self, file_id: str, max_bytes: int):
        """返回（登记行, 原件字节）。不存在、不属于本租户、不是可用状态都报 E2001；超过 max_bytes 报 E2002。"""
        row = self.describe(file_id)
        if row["bytes"] > max_bytes:
            raise EvaError(Code.FILE_UNSUPPORTED, "文件超过读取大小上限", reason="too_large",
                           file_id=file_id, bytes=row["bytes"], max_bytes=max_bytes)
        return row, self._store.get(row["minio_key"])

    def _file(self, file_id: str) -> dict:
        row = Files(self.conn, self.tenant_id).get(file_id)
        if row is None:
            raise EvaError(Code.FILE_NOT_FOUND, "文件不存在", file_id=file_id)
        return row

    def put_overflow(self, session_id: str, data: bytes) -> str:
        key = f"{self._under('cache/overflow')}{session_id}/{ids.new_id()}"
        self._store.put(key, data, "text/plain; charset=utf-8")
        return key

    def put_call_record(self, session_id: str, name: str, record: dict, blobs: Dict[str, str]) -> None:
        """模型调用记录。blobs 是按内容哈希存的大文本（system prompt、工具 schema），同哈希重复写内容不变。"""
        key = f"{self._under('diag/calls')}{_key_part(session_id)}/{_key_part(name)}.json"
        for sha, text in blobs.items():
            self._store.put(f"{self._under('diag/blobs')}{sha}", text.encode("utf-8"), "text/plain; charset=utf-8")
        self._store.put(key, json.dumps(record, ensure_ascii=False, default=str).encode("utf-8"), "application/json")

    def read_call_record(self, session_id: str, name: str) -> dict:
        return json.loads(self._store.get(f"{self._under('diag/calls')}{_key_part(session_id)}/{_key_part(name)}.json"))

    def put_eval_inputs(self, sha: str, data: bytes) -> None:
        """评测认可版本的输入快照（打包后的字节，内容由调用方定），按输入版本哈希存，同哈希重复写内容不变。只写在评测租户上。"""
        tenants.require_realm(self.tenant_id, TenantRealm.EVAL)
        self._store.put(f"{self._prefix()}eval_inputs/{_key_part(sha)}", data, "application/zip")

    def read_eval_inputs(self, sha: str) -> bytes:
        return self._store.get(f"{self._prefix()}eval_inputs/{_key_part(sha)}")

    def read_blob(self, sha: str) -> str:
        return self._store.get(f"{self._under('diag/blobs')}{_key_part(sha)}").decode("utf-8")

    def read_overflow(self, session_id: str, key: str) -> bytes:
        """只能读本租户本会话的溢出产物；其他任何键都当作不存在。"""
        head = f"{self._under('cache/overflow')}{session_id}/"
        tail = key[len(head):] if isinstance(key, str) and key.startswith(head) else ""
        if not tail or "/" in tail or ".." in tail:
            raise EvaError(Code.FILE_NOT_FOUND, "对象不存在", key=str(key))
        return self._store.get(key)
