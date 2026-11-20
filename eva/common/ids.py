"""业务主键生成。ULID 按时间有序，26 位。"""
from ulid import ULID


def new_id() -> str:
    return str(ULID())
