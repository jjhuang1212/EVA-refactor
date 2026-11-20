"""网页上列出和新建评测案例。新建就是写出和手写一样的目录，写完交给 case.load 校验，不合格整个撤销。"""
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

from devtools.eval.case import CASES, Case, load
from devtools.eval.runner import NAME
from eva.contracts.errors import Code, EvaError


def as_dict(c: Case) -> dict:
    return {"name": c.name, "message": c.message, "files": [f.name for f in c.files],
            "reply_contains": c.reply_contains, "reply_not_contains": c.reply_not_contains, "max_steps": c.max_steps,
            "max_questions": c.max_questions, "persona": c.persona,
            "max_cost_cny": None if c.max_cost_cny is None else str(c.max_cost_cny)}


def list_cases(root: Path = CASES) -> List[dict]:
    """案例目录坏了也列出来并带上错误，不能悄悄少一个。"""
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")) if root.is_dir() else []:
        try:
            out.append(as_dict(load(d)))
        except EvaError as e:
            out.append({"name": d.name, "error": e.to_dict()})
    return out


def get_case(name: str, root: Path = CASES) -> Case:
    if not NAME.fullmatch(name):
        raise EvaError(Code.CONFIG_INVALID, "案例名只能含字母、数字、_、-，1 到 40 个字符", name=name)
    if not (root / name / "case.yaml").is_file():
        raise EvaError(Code.FILE_NOT_FOUND, "没有这个案例", case=name)
    return load(root / name)


def create_case(name: str, message: str, files: List[Tuple[str, bytes]],
                reply_contains: List[str], reply_not_contains: List[str], max_steps: Optional[int],
                max_cost_cny: Optional[float], max_questions: Optional[int] = None, persona: Optional[str] = None,
                root: Path = CASES) -> dict:
    if not NAME.fullmatch(name):
        raise EvaError(Code.CONFIG_INVALID, "案例名只能含字母、数字、_、-，1 到 40 个字符", name=name)
    if (root / name).exists():
        raise EvaError(Code.INVALID_VALUE, "案例已存在，换个名字", case=name)
    for filename, _ in files:
        if not filename or Path(filename).name != filename or filename == "case.yaml" or filename.startswith("."):
            raise EvaError(Code.CONFIG_INVALID, "样本文件名不合法", filename=filename)
    asserts = {k: v for k, v in (("reply_contains", reply_contains), ("reply_not_contains", reply_not_contains),
                                 ("max_steps", max_steps), ("max_cost_cny", max_cost_cny), ("max_questions", max_questions))
               if v is not None and v != []}  # 0 要交给校验去判（max_steps 0 不合法，max_questions 0 合法）
    spec = {"message": message, "files": [n for n, _ in files], "asserts": asserts, "persona": persona}
    spec = {k: v for k, v in spec.items() if v}
    tmp = root / f".tmp-{name}"  # 写好并校验通过才改名，失败不留半个案例
    root.mkdir(parents=True, exist_ok=True)
    try:
        tmp.mkdir()
        for filename, data in files:
            (tmp / filename).write_bytes(data)
        (tmp / "case.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
        load(tmp)
        tmp.rename(root / name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return as_dict(load(root / name))
