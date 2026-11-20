"""tenant_skill_versions 表：租户层 skill，只追加。每个 (租户, 名字) 版本号最大的那行生效。只属于这个租户。

写入口只有 put / archive / revert 三个，都在同一事务里先锁租户行（让同租户的并发写入串行，版本号不重复），
再插入新行，最后把 tenants.skills_rev 加一。校验在这里做，坏内容进不了库。
"""
from dataclasses import dataclass
from typing import Callable, FrozenSet, List, Optional, Tuple

from eva.common import clock
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import SKILL_NAME, SKILL_NAME_MAX, SkillKind, SkillStatus, check
from eva.storage import db
from eva.storage.repo import TenantRepo


@dataclass(frozen=True)
class SkillLimits:
    """写入校验用的外部事实：公共层有哪些名字，以及配置里的上限。"""
    global_names: FrozenSet[str]
    max_body_bytes: int
    max_description_chars: int
    max_tenant_skills: int


_LATEST = (
    "SELECT name, version, kind, status, description, body FROM tenant_skill_versions v WHERE tenant_id = %s "
    "AND version = (SELECT MAX(version) FROM tenant_skill_versions WHERE tenant_id = v.tenant_id AND name = v.name)"
)


def _row(r) -> dict:
    return dict(zip(("name", "version", "kind", "status", "description", "body"), r))


class TenantSkillVersions(TenantRepo):
    def rev(self) -> int:
        r = self._execute("SELECT skills_rev FROM tenants WHERE tenant_id = %s", (self.tenant_id,)).fetchone()
        if r is None:
            raise EvaError(Code.TENANT_NOT_FOUND, "租户不存在", tenant_id=self.tenant_id)
        return r[0]

    def snapshot(self) -> Tuple[int, List[dict]]:
        """修订号和当前生效（active）的 skill，同一个事务里读，两者对得上。"""
        rev = self.rev()
        rows = self._execute(_LATEST + " ORDER BY name", (self.tenant_id,)).fetchall()
        return rev, [d for d in map(_row, rows) if d["status"] == SkillStatus.ACTIVE.value]

    def history(self, name: str) -> List[dict]:
        rows = self._execute(
            "SELECT name, version, kind, status, description, body FROM tenant_skill_versions "
            "WHERE tenant_id = %s AND name = %s ORDER BY version", (self.tenant_id, name)).fetchall()
        return [_row(r) for r in rows]

    def put(self, name: str, kind: str, body: str, description: Optional[str], reason: Optional[str],
            limits: SkillLimits) -> int:
        """新增或修改一个 skill（归档过的会恢复），返回新版本号。"""
        kind = check(SkillKind, kind)
        self._check_content(name, kind, body, description, reason, limits)
        self._lock_tenant()
        others = {d["name"] for d in self._latest() if d["status"] == SkillStatus.ACTIVE.value and d["name"] != name}
        if len(others) + 1 > limits.max_tenant_skills:
            raise EvaError(Code.INVALID_VALUE, "该租户的 skill 数量到达上限，先归档不用的",
                           max_tenant_skills=limits.max_tenant_skills)
        return self._append(name, kind.value, SkillStatus.ACTIVE.value, description, body, reason)

    def archive(self, name: str, reason: Optional[str]) -> int:
        """归档：追加一行 archived，内容沿用最后一版。已归档或不存在抛 E0001。"""
        self._lock_tenant()
        last = self._last(name)
        if last["status"] != SkillStatus.ACTIVE.value:
            raise EvaError(Code.INVALID_VALUE, "这个租户 skill 已经归档", name=name)
        return self._append(name, last["kind"], SkillStatus.ARCHIVED.value, last["description"], last["body"], reason)

    def revert(self, name: str, to_version: int, reason: Optional[str], limits: SkillLimits) -> int:
        """回退：追加一行 active，内容取自 to_version，并重新过一遍 put 的校验（公共层可能已经变了）。"""
        old = [d for d in self.history(name) if d["version"] == to_version]
        if not old:
            raise EvaError(Code.INVALID_VALUE, "没有这个版本", name=name, version=to_version)
        return self.put(name, old[0]["kind"], old[0]["body"], old[0]["description"], reason, limits)

    def _check_content(self, name, kind: SkillKind, body, description, reason, limits: SkillLimits) -> None:
        if not isinstance(name, str) or not SKILL_NAME.fullmatch(name) or len(name) > SKILL_NAME_MAX:
            raise EvaError(Code.INVALID_VALUE, "skill 名只能是小写字母、数字、- 和 _，以字母开头", name=repr(name))
        if kind is SkillKind.ADDENDUM and name not in limits.global_names:
            raise EvaError(Code.INVALID_VALUE, "补充必须对应一个公共 skill，公共层没有这个名字", name=name)
        if kind is SkillKind.OWN and name in limits.global_names:
            raise EvaError(Code.INVALID_VALUE, "租户独有的 skill 不能和公共 skill 重名", name=name)
        if not isinstance(body, str) or not body.strip():
            raise EvaError(Code.INVALID_VALUE, "skill 正文不能为空", name=name)
        if len(body.encode("utf-8")) > limits.max_body_bytes:
            raise EvaError(Code.INVALID_VALUE, "skill 正文超过上限", name=name, max_bytes=limits.max_body_bytes)
        if kind is SkillKind.ADDENDUM and description is not None:
            raise EvaError(Code.INVALID_VALUE, "补充不带 description，清单沿用公共 skill 的", name=name)
        if kind is SkillKind.OWN and (not isinstance(description, str) or not description.strip()
                                      or len(description) > limits.max_description_chars):
            raise EvaError(Code.INVALID_VALUE, "租户独有的 skill 需要非空且不超长的 description", name=name,
                           max_chars=limits.max_description_chars)
        if reason is not None and (not isinstance(reason, str) or len(reason) > 512):
            raise EvaError(Code.INVALID_VALUE, "reason 必须是不超过 512 字的文字", name=name)

    def _lock_tenant(self) -> None:
        """先改租户行拿到行锁，同租户的写入排队；顺便把修订号加一，写入失败整个事务回滚，修订号不会白涨。"""
        n = self._execute("UPDATE tenants SET skills_rev = skills_rev + 1 WHERE tenant_id = %s", (self.tenant_id,)).rowcount
        if n == 0:
            raise EvaError(Code.TENANT_NOT_FOUND, "租户不存在", tenant_id=self.tenant_id)

    def _latest(self) -> List[dict]:
        return [_row(r) for r in self._execute(_LATEST, (self.tenant_id,)).fetchall()]

    def _last(self, name: str) -> dict:
        history = self.history(name)
        if not history:
            raise EvaError(Code.INVALID_VALUE, "该租户没有这个 skill", name=name)
        return history[-1]

    def _append(self, name, kind, status, description, body, reason) -> int:
        version = self._execute("SELECT ISNULL(MAX(version), 0) + 1 FROM tenant_skill_versions WHERE tenant_id = %s AND name = %s",
                                (self.tenant_id, name)).fetchone()[0]
        self._execute(
            "INSERT INTO tenant_skill_versions (tenant_id, name, version, kind, status, description, body, reason, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (self.tenant_id, name, version, kind, status, description, body, reason, clock.now()))
        return version


class TenantSkills:
    """绑定一个租户的读写端:读给 SkillView 叠加用(rev、snapshot),写只有 put / archive / revert 三个入口。
    limits 每次写入时现取,公共层的名单随热加载变。"""

    def __init__(self, tenant_id: str, limits: Callable[[], SkillLimits], open_tx: Callable = db.transaction):
        self.tenant_id, self._limits, self._open_tx = tenant_id, limits, open_tx

    def rev(self) -> int:
        with self._open_tx() as conn:
            return TenantSkillVersions(conn, self.tenant_id).rev()

    def snapshot(self) -> Tuple[int, List[dict]]:
        with self._open_tx() as conn:
            return TenantSkillVersions(conn, self.tenant_id).snapshot()

    def put(self, name: str, kind: str, body: str, description: Optional[str] = None, reason: Optional[str] = None) -> int:
        with self._open_tx() as conn:
            return TenantSkillVersions(conn, self.tenant_id).put(name, kind, body, description, reason, self._limits())

    def archive(self, name: str, reason: Optional[str] = None) -> int:
        with self._open_tx() as conn:
            return TenantSkillVersions(conn, self.tenant_id).archive(name, reason)

    def revert(self, name: str, to_version: int, reason: Optional[str] = None) -> int:
        with self._open_tx() as conn:
            return TenantSkillVersions(conn, self.tenant_id).revert(name, to_version, reason, self._limits())