"""SQL Server 连接。数据库客户端只在存储层持有。"""
import os
from contextlib import contextmanager

import pymssql
from dotenv import load_dotenv

from eva.contracts.errors import Code, EvaError

LEGACY_DATABASE = "ccm"

# 连不上、超时、死锁牺牲者，换个时机重试可能成功；登录失败等不在其中
_UNAVAILABLE = {20002, 20003, 20009, 1205}


def translate(e: Exception) -> EvaError:
    first = e.args[0] if e.args else None
    if isinstance(first, tuple):  # 连接类错误把 (编号, 消息) 包成一个元组
        first = first[0] if first else None
    number = first if isinstance(first, int) else None
    if isinstance(e, pymssql.OperationalError) and number in _UNAVAILABLE:
        return EvaError(Code.STORAGE_UNAVAILABLE, str(e), error_type=type(e).__name__)
    return EvaError(Code.STORAGE_ERROR, str(e), error_type=type(e).__name__)


def _connect(user_var: str, pass_var: str, database_var: str = "", database: str = ""):
    load_dotenv()
    required = ["DB_SERVER", user_var, pass_var] + ([database_var] if database_var else [])
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise EvaError(Code.STORAGE_ERROR, "缺少环境变量", missing=missing)
    # DB_SERVER 写成 "主机,端口" 或 "主机:端口"，pymssql 要拆开传
    host, _, port = os.environ["DB_SERVER"].replace(",", ":").partition(":")
    try:
        return pymssql.connect(
            server=host,
            port=port or "1433",
            user=os.environ[user_var],
            password=os.environ[pass_var],
            database=os.environ[database_var] if database_var else database,
            charset="UTF-8",
        )
    except pymssql.Error as e:
        raise translate(e) from e


def connect():
    """新库 eva_perf，运行时代码只用这个。"""
    return _connect("EVA_DB_USER", "EVA_DB_PASS", database_var="EVA_DB_NAME")


def connect_legacy():
    """旧库 ccm，只给一次性迁移脚本读数据，不得写入。"""
    return _connect("LEGACY_DB_USER", "LEGACY_DB_PASS", database=LEGACY_DATABASE)


@contextmanager
def transaction():
    """独立事务：正常退出提交，异常回滚，最后关连接。仓储本身不提交，由持有它的一方用这个收尾。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

