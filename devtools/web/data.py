"""网页读会话数据的三个入口。数据库和对象存储的客户端都在存储层里，这里只调它们；换成假的就能不连库测接口。"""
from pathlib import Path
from typing import List, Optional, Tuple

from devtools.eval import compare, marks, usersim
from devtools.replay import timeline
from eva.app import SYSTEM_PROMPT_PATH
from eva.loop import config as loop_config
from eva.storage import db
from eva.storage.developer import Developer
from eva.storage.objects import ObjectStore
from eva.storage.sessions import Sessions


def tenants(prefix: Optional[str], limit: int, search: Optional[str] = None, offset: int = 0) -> Tuple[List[dict], bool]:
    with db.transaction() as conn:
        return Developer(conn).tenants(prefix, limit, search, offset)


def sessions(tenant_id: str, limit: int, offset: int = 0) -> Tuple[List[dict], bool]:
    with db.transaction() as conn:
        return Sessions(conn, tenant_id).list_recent(limit, offset)


def replay(tenant_id: str, session_id: str, full: bool) -> dict:
    with db.transaction() as conn:
        return timeline.build(conn, ObjectStore.from_config(), tenant_id, session_id, with_blobs=full)


def defaults() -> dict:
    """评测页预填用：磁盘上现在生效的思考配置、系统提示词，以及模拟用户的默认提示词和最多回答轮数。"""
    cfg = loop_config.load()
    return {"think_first_steps": cfg.think_first_steps, "think_after_tools": list(cfg.think_after_tools),
            "max_steps": cfg.max_steps, "system_prompt": SYSTEM_PROMPT_PATH.read_text(encoding="utf-8"),
            **usersim.load_config()}


def human_verdicts(case: str) -> dict:
    return compare.human_verdicts(case)


def annotate(case: str, run_id: str, verdict: str, note: Optional[str], out_root: Path) -> dict:
    return marks.annotate(case, run_id, verdict, note, out_root)


def mark_version(case: str, run_id: str, name: str, note: Optional[str], out_root: Path) -> dict:
    return marks.mark_version(case, run_id, name, note, out_root)


def versions() -> List[dict]:
    return marks.list_versions()


def version(name: str) -> dict:
    got = marks.fetch_version(name)
    return {**{k: v for k, v in got.items() if k != "files"}, "files": sorted(got["files"])}
