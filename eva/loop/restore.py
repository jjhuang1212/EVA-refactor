"""压缩时要重新放回视图的信息：本会话加载过的 skill（取压缩时的当前版本）和本租户最近登记的文件。

取数的函数由装配方传入，loop 层不碰存储。结果写进 compaction_end 的 attachments，视图只读日志，
所以回放和审计看到的就是当时放回去的内容。
"""
import hashlib
import json
from typing import Callable, List, Optional

from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import EventType
from eva.loop import view


def loaded_skills(events: List[dict], tool_name: str) -> List[str]:
    """最近一次 context_reset 之后成功读过正文的 skill 名，最近读的在前，去重。只读参考文件的不算。"""
    start = 0
    for i, e in enumerate(events):
        if e["event_type"] == EventType.CONTEXT_RESET.value:
            start = i + 1
    events = events[start:]
    succeeded = {e["payload"].get("call_id") for e in events
                 if e["event_type"] == EventType.TOOL_RESULT.value and not e["payload"].get("is_error")}
    names: List[str] = []
    for e in reversed(events):
        p = e["payload"]
        if e["event_type"] != EventType.TOOL_CALL.value or p.get("name") != tool_name or p.get("call_id") not in succeeded:
            continue
        args = p.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                continue
        if isinstance(args, dict) and "file" not in args and isinstance(args.get("name"), str) and args["name"] not in names:
            names.append(args["name"])
    return names


def _with_prior(names: List[str], events: List[dict]) -> List[str]:
    """events 只从上次压缩的切点起读，更早加载的 skill 靠上次压缩结束事件里放回过的名单接上，连续压缩不会丢。"""
    att = (view.visible(events)[3] or {"payload": {}})["payload"].get("attachments") or {}
    older = [s["name"] for s in att.get("skills", [])] + list(att.get("skills_omitted", []))
    return names + [n for n in older if n not in names]


def build(events: List[dict], cut_seq: int, cfg, tool_name: str, read_skill: Callable[[str], str],
          recent_files: Callable[[int], List[dict]]) -> dict:
    """切点之前加载过、切点之后没有再加载的 skill，读它们现在的正文；加上最近的文件清单。
    skill 现在已经不存在的记进 skills_missing，总量超限放不下的记进 skills_omitted，都让模型看得到。"""
    head = [e for e in events if e["seq"] < cut_seq]
    tail = [e for e in events if e["seq"] >= cut_seq]
    kept = set(loaded_skills(tail, tool_name))
    skills, omitted, missing, used = [], [], [], 0
    for name in _with_prior(loaded_skills(head, tool_name), events):
        if name in kept:
            continue
        try:
            text = read_skill(name)
        except EvaError as e:
            if e.code is not Code.TOOL_ARGS_INVALID:  # 其余是读取本身出了问题，不能当成 skill 不存在
                raise
            missing.append(name)
            continue
        size = len(text.encode("utf-8"))
        if used + size > cfg.restore_skills_total_bytes:
            omitted.append(name)
            continue
        used += size
        skills.append({"name": name, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text})
    return {"skills": skills, "skills_omitted": omitted, "skills_missing": missing,
            "files": recent_files(cfg.restore_files_max) if cfg.restore_files_max else []}
