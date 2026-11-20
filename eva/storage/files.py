"""file_registry 表。对象键由存储层按 tenant_id 拼出，业务代码不拼。"""
import posixpath
import re
from typing import Optional

from eva.common import clock, ids
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import FileStatus, check
from eva.storage.repo import UNIQUE_VIOLATION, TenantRepo

_NAMES = ("file_id", "tenant_id", "sha256", "filename", "mime", "bytes", "minio_key", "parsed_key", "status",
          "created_at")
_COLS = ", ".join(_NAMES)
_SHA256 = re.compile(r"[0-9a-f]{64}")


class Files(TenantRepo):
    _MAX_RENAMES = 99

    def get(self, file_id: str) -> Optional[dict]:
        return self._one("file_id = %s", file_id)

    def find_by_sha(self, sha256: str) -> Optional[dict]:
        return self._one("sha256 = %s", sha256)

    def available_named(self, filename: str) -> list:
        rows = self._execute(f"SELECT {_COLS} FROM file_registry WHERE tenant_id = %s AND filename = %s AND status = %s "
                             "ORDER BY created_at DESC", (self.tenant_id, filename, FileStatus.AVAILABLE.value)).fetchall()
        return [dict(zip(_NAMES, r)) for r in rows]

    def recent(self, limit: int) -> list:
        """本租户最近登记的可用文件，新的在前，只取清单需要的几列。没有就返回空列表。"""
        rows = self._execute("SELECT TOP (%s) file_id, filename, mime, bytes FROM file_registry WHERE tenant_id = %s "
                             "AND status = %s ORDER BY created_at DESC, file_id DESC",
                             (limit, self.tenant_id, FileStatus.AVAILABLE.value)).fetchall()
        return [dict(zip(("file_id", "filename", "mime", "bytes"), r)) for r in rows]

    def _one(self, cond: str, value) -> Optional[dict]:
        r = self._execute(f"SELECT {_COLS} FROM file_registry WHERE tenant_id = %s AND {cond}",
                          (self.tenant_id, value)).fetchone()
        return dict(zip(_NAMES, r)) if r else None

    def register(self, sha256: str, filename: str, mime: str, size: int) -> dict:
        """登记上传。本租户已有同内容的行则更新原行（文件名、类型、大小、状态回到 uploaded），沿用 file_id 和对象键。
        文件名在租户内不重复：别的内容已用了这个名字，就改成"名字(2).扩展名"，引用靠文件名定位到唯一一份。"""
        if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
            raise EvaError(Code.INVALID_VALUE, "sha256 必须是 64 位小写十六进制", value=repr(sha256))
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise EvaError(Code.INVALID_VALUE, "size 必须是非负整数", value=repr(size))
        uploaded = FileStatus.UPLOADED.value
        file_id = ids.new_id()
        key = self._object_key(file_id)
        # 并发上传抢同一个名字时，唯一索引会拒绝后到的一方；重新挑名字再写
        taken = EvaError(Code.STORAGE_ERROR, "文件名被并发上传占用")
        clash = {n: taken for n in UNIQUE_VIOLATION}
        for _ in range(self._MAX_RENAMES + 1):
            name = self._free_name(filename, sha256)
            try:
                # 判断和插入在同一条语句里，并发首传同一文件只有一方插入；随后统一更新，首传时等于写同样的值
                self._execute(
                    f"INSERT INTO file_registry ({_COLS}) SELECT %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s "
                    "WHERE NOT EXISTS (SELECT 1 FROM file_registry WITH (UPDLOCK, HOLDLOCK) "
                    "WHERE tenant_id = %s AND sha256 = %s)",
                    (file_id, self.tenant_id, sha256, name, mime, size, key, uploaded, clock.now(), self.tenant_id, sha256),
                    clash)
                self._execute(
                    "UPDATE file_registry SET filename = %s, mime = %s, bytes = %s, status = %s "
                    "WHERE tenant_id = %s AND sha256 = %s",
                    (name, mime, size, uploaded, self.tenant_id, sha256), clash)
            except EvaError as e:
                if e is not taken:
                    raise
                continue
            return self.find_by_sha(sha256)
        raise EvaError(Code.INVALID_VALUE, "同名文件太多，无法生成不重复的文件名", filename=filename)

    def _free_name(self, filename: str, sha256: str) -> str:
        """别的内容（哈希不同、未删除）没占用的文件名。"""
        base, ext = posixpath.splitext(filename)
        for n in range(1, self._MAX_RENAMES + 2):
            candidate = filename if n == 1 else f"{base}({n}){ext}"
            taken = self._execute("SELECT 1 FROM file_registry WHERE tenant_id = %s AND filename = %s AND sha256 <> %s "
                                  "AND status <> %s", (self.tenant_id, candidate, sha256, FileStatus.DELETED.value)).fetchone()
            if taken is None:
                return candidate
        raise EvaError(Code.INVALID_VALUE, "同名文件太多，无法生成不重复的文件名", filename=filename)

    def _object_key(self, file_id: str) -> str:
        if self._execute("SELECT 1 FROM tenants WHERE tenant_id = %s", (self.tenant_id,)).fetchone() is None:
            raise EvaError(Code.TENANT_NOT_FOUND, "租户不存在", tenant_id=self.tenant_id)
        return f"t/{self.tenant_id}/files/{file_id}"

    def set_status(self, file_id: str, status: str) -> None:
        self._update(file_id, "status = %s", check(FileStatus, status).value)

    def set_parsed_key(self, file_id: str, parsed_key: str) -> None:
        if not parsed_key:
            raise EvaError(Code.INVALID_VALUE, "parsed_key 不能为空")
        self._update(file_id, "parsed_key = %s", parsed_key)

    def _update(self, file_id: str, assignment: str, value) -> None:
        cur = self._execute(
            f"UPDATE file_registry SET {assignment} WHERE tenant_id = %s AND file_id = %s",
            (value, self.tenant_id, file_id),
        )
        if cur.rowcount != 1:
            raise EvaError(Code.FILE_NOT_FOUND, "文件登记不存在", file_id=file_id)

