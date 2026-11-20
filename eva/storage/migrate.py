"""迁移执行器：按序号执行 migrations/ 下还没跑过的脚本。

表变更只走这里，不手工改库。已执行脚本的内容不得再改，改了要新增脚本。
两个进程同时迁移不做互斥，后到者会因表已存在而失败。
"""
import hashlib
import re
from pathlib import Path
from typing import Dict, List, NamedTuple

import pymssql

from eva.common import clock
from eva.contracts.errors import Code, EvaError
from eva.storage.db import translate

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

_ENSURE_LEDGER = """
IF OBJECT_ID('schema_migrations', 'U') IS NULL
CREATE TABLE schema_migrations (
    version    int          NOT NULL CONSTRAINT pk_schema_migrations PRIMARY KEY,
    name       varchar(100) NOT NULL,
    checksum   char(64)     NOT NULL,
    applied_at datetime2    NOT NULL
)
"""


class Migration(NamedTuple):
    version: int
    name: str
    sql: str
    checksum: str


def discover(directory: Path = MIGRATIONS_DIR) -> List[Migration]:
    found = []
    for p in sorted(directory.glob("*.sql")):
        m = _NAME_RE.match(p.name)
        if not m:
            raise EvaError(Code.MIGRATION_DRIFT, "迁移脚本文件名必须是 NNNN_小写名.sql", file=p.name)
        raw = p.read_bytes().replace(b"\r\n", b"\n")  # 换行符不同不算内容变化
        found.append(Migration(int(m.group(1)), p.name, raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()))
    return found


def plan(migrations: List[Migration], applied: Dict[int, str]) -> List[Migration]:
    """返回要执行的脚本。历史与脚本对不上就抛错，不猜。"""
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise EvaError(Code.MIGRATION_DRIFT, "迁移序号必须从 1 连续递增", versions=versions)
    by_version = {m.version: m for m in migrations}
    for v, checksum in applied.items():
        if v not in by_version:
            raise EvaError(Code.MIGRATION_DRIFT, "库里记录的迁移在脚本目录中不存在", version=v)
        if by_version[v].checksum != checksum:
            raise EvaError(Code.MIGRATION_DRIFT, "已执行的迁移脚本被修改过", version=v, file=by_version[v].name)
    pending = [m for m in migrations if m.version not in applied]
    if applied and pending and pending[0].version < max(applied):
        raise EvaError(Code.MIGRATION_DRIFT, "存在编号小于已执行最大编号的未执行脚本", version=pending[0].version)
    return pending


def run(conn, directory: Path = MIGRATIONS_DIR) -> List[int]:
    """执行全部待执行脚本，返回本次执行的版本号。每个脚本一个事务。"""
    migrations = discover(directory)
    try:
        cur = conn.cursor()
        cur.execute(_ENSURE_LEDGER)
        conn.commit()
        cur.execute("SELECT version, checksum FROM schema_migrations")
        applied = {v: c for v, c in cur.fetchall()}
        done = []
        for m in plan(migrations, applied):
            try:
                cur.execute(m.sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (%s, %s, %s, %s)",
                    (m.version, m.name, m.checksum, clock.now()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            done.append(m.version)
        return done
    except pymssql.Error as e:
        raise translate(e) from e
