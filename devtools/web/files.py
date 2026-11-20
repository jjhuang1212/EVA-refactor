"""网页上可改的文件：系统提示词、configs/ 顶层的 yaml、skills/ 下的文本文件。

直接改真实工作区。保存要带打开时的哈希，磁盘上已不一样就拒绝（IDE 里改过），不静默覆盖。
保留原文件的换行风格和 BOM；先写临时文件再替换，不留写一半的文件。
"""
import hashlib
import os
import re
from pathlib import Path, PurePosixPath
from typing import List, Optional

import yaml

from devtools.eval.snapshot import ROOT
from eva.contracts.errors import Code, EvaError

_BOM = b"\xef\xbb\xbf"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _target(rel, root: Path) -> Path:
    """校验路径在白名单内，返回磁盘路径。符号链接和越界一律拒绝。"""
    bad = lambda why: EvaError(Code.CONFIG_INVALID, f"这个路径不能在网页上改：{why}", path=repr(rel))
    if not isinstance(rel, str) or not rel or "\\" in rel or "\0" in rel:
        raise bad("格式不对")
    parts = PurePosixPath(rel).parts
    if any(p in ("", ".", "..") for p in parts) or PurePosixPath(rel).is_absolute() or ":" in parts[0]:
        raise bad("含有 . 或 ..、或是绝对路径")
    if rel == "configs/system_prompt.md" or (len(parts) == 2 and parts[0] == "configs" and rel.endswith(".yaml")):
        top = "configs"
    elif parts[0] == "skills" and len(parts) >= 2 and "__pycache__" not in parts:
        top = "skills"
    else:
        raise bad("只能改 configs/system_prompt.md、configs/ 顶层的 .yaml 和 skills/ 下的文件")
    p = root / rel
    if p.is_symlink() or not p.resolve().is_relative_to((root / top).resolve()):
        raise bad("是符号链接或指向目录外")
    return p


def _text(raw: bytes, rel: str) -> str:
    try:
        return raw.decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeDecodeError as e:
        raise EvaError(Code.FILE_UNSUPPORTED, "不是 UTF-8 文本，网页上不能改", path=rel) from e


def list_files(root: Path = ROOT) -> List[dict]:
    out = []
    for top in ("configs", "skills"):
        for p in sorted((root / top).rglob("*")):
            rel = p.relative_to(root).as_posix()
            if not p.is_file():
                continue
            try:
                _target(rel, root)
                raw = p.read_bytes()
                _text(raw, rel)
            except EvaError as e:
                if e.code in (Code.CONFIG_INVALID, Code.FILE_UNSUPPORTED):
                    continue  # 不在白名单或不是文本的，列表里不出现
                raise
            out.append({"path": rel, "sha": sha(raw)})
    return out


def read_file(rel: str, root: Path = ROOT) -> dict:
    p = _target(rel, root)
    if not p.is_file():
        raise EvaError(Code.FILE_NOT_FOUND, "没有这个文件", path=rel)
    raw = p.read_bytes()
    return {"path": rel, "content": _text(raw, rel), "sha": sha(raw)}


def _set_line(text: str, key: str, value: str) -> str:
    """把 yaml 里顶层 `key: ...` 这一行换成 `key: value`，保留其余内容和注释。找不到恰好一行就报错，不猜。"""
    new, n = re.subn(rf"^{key}:.*$", lambda _: f"{key}: {value}", text, flags=re.M)
    if n != 1:
        raise EvaError(Code.CONFIG_INVALID, "loop.yaml 里找不到唯一的这一项，不能自动改写", key=key, found=n)
    return new


def adopt(overrides: dict, base: dict, root: Path = ROOT) -> List[dict]:
    """把一次运行的覆盖项写回真文件。运行开始时记下了两个文件的哈希（base），磁盘上现在不一样就拒绝（FILE_CHANGED），
    因为覆盖是在那个版本上试出来的。先全部算好、校验过再写，不会只写一半。"""
    plan = []  # (相对路径, 新内容, 读到时的哈希)
    if "system_prompt" in overrides:
        plan.append(("configs/system_prompt.md", lambda old: overrides["system_prompt"]))
    loop = {k: overrides[k] for k in ("think_first_steps", "think_after_tools", "max_steps") if k in overrides}
    if loop:
        def loop_yaml(old: str) -> str:
            for key in ("think_first_steps", "max_steps"):
                if key in loop:
                    old = _set_line(old, key, str(loop[key]))
            if "think_after_tools" in loop:
                old = _set_line(old, "think_after_tools", "[" + ", ".join(loop["think_after_tools"]) + "]")
            return old
        plan.append(("configs/loop.yaml", loop_yaml))
    if not plan:
        raise EvaError(Code.INVALID_VALUE, "这次运行没有可采用的覆盖项")
    ready = []
    for rel, make in plan:
        cur = read_file(rel, root)
        if base.get(rel) is None:
            raise EvaError(Code.INVALID_VALUE, "这次运行没有记录当时文件的哈希（早期运行），不能采用", path=rel)
        if cur["sha"] != base[rel]:
            raise EvaError(Code.FILE_CHANGED, "这个文件在那次运行之后被改过，不能直接采用，先在文件页处理", path=rel)
        new = make(cur["content"])
        if rel.endswith(".yaml"):
            try:
                yaml.safe_load(new)
            except yaml.YAMLError as e:
                raise EvaError(Code.CONFIG_INVALID, "改写后的 yaml 语法有误，没有写入", path=rel, reason=str(e)) from e
        ready.append((rel, new, cur["sha"]))
    return [write_file(rel, new, sha_, root) for rel, new, sha_ in ready]


def write_file(rel: str, content, base_sha: Optional[str], root: Path = ROOT) -> dict:
    """base_sha 是打开时的哈希；新建 skills/ 下的文件时不带。返回新的哈希。"""
    if not isinstance(content, str):
        raise EvaError(Code.INVALID_VALUE, "content 必须是字符串")
    p = _target(rel, root)
    if p.is_file():
        raw = p.read_bytes()
        if base_sha != sha(raw):
            raise EvaError(Code.FILE_CHANGED, "文件在你打开之后被改过，请重新读取再改", path=rel)
        bom, crlf = raw.startswith(_BOM), b"\r\n" in raw
    else:
        if base_sha:
            raise EvaError(Code.FILE_CHANGED, "文件已不存在，请重新读取", path=rel)
        if not rel.startswith("skills/"):
            raise EvaError(Code.FILE_NOT_FOUND, "configs/ 下不能新建文件", path=rel)
        bom = crlf = False
    text = content.replace("\r\n", "\n")
    if rel.endswith(".yaml"):
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise EvaError(Code.CONFIG_INVALID, "yaml 语法有误，没有保存", path=rel, reason=str(e)) from e
    data = (_BOM if bom else b"") + (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".tmp-{os.getpid()}-{p.name}")
    tmp.write_bytes(data)
    os.replace(tmp, p)
    return {"path": rel, "sha": sha(data)}
