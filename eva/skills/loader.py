"""skill 的加载与校验。skill 是 skills/<名字>/ 下的一份 SKILL.md（判断和步骤），加可选的 references/（按需读的参考文档，
只收 .md）和 scripts/（给沙箱用的脚本）。

启动时全部读入并校验，任何一个不合格都抛 CONFIG_INVALID，不跳过：少了一个 skill 的服务看着正常，模型却悄悄少了一项能力。
模型靠系统提示里的清单知道有哪些 skill，用 load_skill 读正文，再按正文的指引读 references；脚本由 run_python 放进沙箱的工作目录。

运行中改了 skills/ 下的文件不用重启：SkillRegistry 按间隔比对文件指纹，变了就重新加载。新内容不合格时保留上一份合格的，
并报一次错误（显式降级，因为开发时改到一半的文件很常见，不能让所有对话跟着失败）。每个对话持有一个 SkillView，
在每个 turn 开头取一次快照，一个 turn 内清单、正文和脚本来自同一份。
"""
import difflib
import logging
import re
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

import yaml

from eva.common import clock, logs
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import SKILL_NAME, SKILL_NAME_MAX, SkillKind

log =logging.getLogger("eva.skills")

ROOT = Path(__file__).resolve().parents[2] / "skills"
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "configs" / "skills.yaml"
SCRIPTS_DIR = "scripts"
REFERENCES_DIR = "references"
STAGE_DIR = "skills"  # 脚本在沙箱工作目录里的位置：skills/<名字>/<scripts 下的相对路径>

_FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", re.S)


@dataclass(frozen=True)
class SkillsConfig:
    max_body_bytes: int
    max_reference_bytes: int
    max_description_chars: int
    max_tenant_skills: int
    max_scripts_bytes: int
    check_interval_seconds: float


def load_config(path: Path = DEFAULT_PATH) -> SkillsConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了 skill 配置", path=str(path), error=str(e)) from e
    if not isinstance(raw, dict):
        raise EvaError(Code.CONFIG_INVALID, "skill 配置顶层必须是映射", path=str(path))
    values = {}
    for key in ("max_body_bytes", "max_reference_bytes", "max_description_chars", "max_tenant_skills", "max_scripts_bytes"):
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise EvaError(Code.CONFIG_INVALID, f"{key} 必须是正整数", value=repr(v))
        values[key] = v
    interval = raw.get("check_interval_seconds")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < 0:
        raise EvaError(Code.CONFIG_INVALID, "check_interval_seconds 必须是不小于 0 的数", value=repr(interval))
    return SkillsConfig(**values, check_interval_seconds=interval)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    references: Mapping[str, str]  # references/ 下的相对路径（用 / 分隔）-> 文本
    scripts: Mapping[str, bytes]  # scripts/ 下的相对路径（用 / 分隔）-> 内容


def render(skill: Skill) -> str:
    """模型读到的 skill：正文，加上参考文件清单和脚本路径提示。load_skill 返回它，压缩后放回视图的也是它。"""
    lines = [skill.body]
    if skill.references:
        lines += ["", "参考文件（用到哪个，调用 load_skill 时带上 name 和 file 再读）："]
        lines += [f"- {rel}" for rel in skill.references]
    if skill.scripts:
        lines += ["", f"脚本（在 run_python 的工作目录里，要 import 时先 sys.path.insert(0, \"{STAGE_DIR}/{skill.name}\")）："]
        lines += [f"- {STAGE_DIR}/{skill.name}/{rel}" for rel in skill.scripts]
    return "\n".join(lines)


class Skills:
    def __init__(self, skills: List[Skill] = ()):
        self._by_name: Dict[str, Skill] = {s.name: s for s in skills}

    def __bool__(self) -> bool:
        return bool(self._by_name)

    def names(self) -> List[str]:
        return list(self._by_name)

    def catalog(self) -> str:
        """写进系统提示的清单：每个 skill 一行名字加描述。没有 skill 时为空串。"""
        return "\n".join(f"- {s.name}：{' '.join(s.description.split())}" for s in self._by_name.values())

    def get(self, name: str) -> Skill:
        """没有这个 skill 抛 E6003（可重试），details 里带相近的名字，让模型改名重试。"""
        try:
            return self._by_name[name]
        except KeyError:
            raise EvaError(Code.TOOL_ARGS_INVALID, "没有这个 skill", name=name,
                           similar=difflib.get_close_matches(str(name), list(self._by_name), n=3),
                           available=list(self._by_name)) from None

    def reference(self, name: str, file: str) -> str:
        """没有这个文件抛 E6003（可重试），details 里带相近的文件名和全部可读的文件名。"""
        refs = self.get(name).references
        try:
            return refs[file]
        except KeyError:
            raise EvaError(Code.TOOL_ARGS_INVALID, "这个 skill 没有这个参考文件", name=name, file=file,
                           similar=difflib.get_close_matches(str(file), list(refs), n=3), available=list(refs)) from None

    def script_files(self) -> Dict[str, bytes]:
        """所有 skill 的脚本，键是沙箱工作目录里的相对路径 skills/<名字>/<相对路径>。"""
        return {f"{STAGE_DIR}/{s.name}/{rel}": data for s in self._by_name.values() for rel, data in s.scripts.items()}


def _read_skill(directory: Path, cfg: SkillsConfig) -> Skill:
    name, md = directory.name, directory / "SKILL.md"
    if not SKILL_NAME.fullmatch(name) or len(name) > SKILL_NAME_MAX:
        raise EvaError(Code.CONFIG_INVALID, "skill 目录名只能是小写字母、数字、- 和 _，且以字母开头", directory=str(directory))
    try:
        text = md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到 SKILL.md", path=str(md), error=str(e)) from e
    match = _FRONT.match(text)
    if match is None:
        raise EvaError(Code.CONFIG_INVALID, "SKILL.md 必须以 --- 包住的 frontmatter 开头", path=str(md))
    try:
        front = yaml.safe_load(match.group(1))
    except yaml.YAMLError as e:
        raise EvaError(Code.CONFIG_INVALID, "SKILL.md 的 frontmatter 不是合法 YAML", path=str(md), error=str(e)) from e
    if not isinstance(front, dict) or front.get("name") != name:
        raise EvaError(Code.CONFIG_INVALID, "SKILL.md 的 name 必须等于目录名", path=str(md), name=repr((front or {}).get("name") if isinstance(front, dict) else front))
    description, body = front.get("description"), match.group(2).strip()
    if not isinstance(description, str) or not description.strip():
        raise EvaError(Code.CONFIG_INVALID, "SKILL.md 缺 description", path=str(md))
    if not body:
        raise EvaError(Code.CONFIG_INVALID, "SKILL.md 正文为空", path=str(md))
    if len(description.strip()) > cfg.max_description_chars:
        raise EvaError(Code.CONFIG_INVALID, "SKILL.md 的 description 超过上限", path=str(md), max_chars=cfg.max_description_chars)
    if len(body.encode("utf-8")) > cfg.max_body_bytes:
        raise EvaError(Code.CONFIG_INVALID, "SKILL.md 正文超过上限，把细节拆进 references/ 由正文指引按需读", path=str(md), max_bytes=cfg.max_body_bytes)
    return Skill(name, description.strip(), body, _read_references(directory / REFERENCES_DIR, cfg),
                 _read_scripts(directory / SCRIPTS_DIR, cfg))


def _read_references(directory: Path, cfg: SkillsConfig) -> Dict[str, str]:
    if not directory.is_dir():
        return {}
    refs: Dict[str, str] = {}
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        # 脚本和脚本用的数据放 scripts/，references/ 里出现别的文件多半是放错了位置
        if path.suffix != ".md":
            raise EvaError(Code.CONFIG_INVALID, "references/ 下只能放 .md 文件", path=str(path))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise EvaError(Code.CONFIG_INVALID, "读不到参考文件", path=str(path), error=str(e)) from e
        if not text.strip():
            raise EvaError(Code.CONFIG_INVALID, "参考文件内容为空", path=str(path))
        if len(text.encode("utf-8")) > cfg.max_reference_bytes:
            raise EvaError(Code.CONFIG_INVALID, "参考文件超过上限", path=str(path), max_bytes=cfg.max_reference_bytes)
        refs[path.relative_to(directory).as_posix()] = text
    return refs


def _read_scripts(directory: Path, cfg: SkillsConfig) -> Dict[str, bytes]:
    if not directory.is_dir():
        return {}
    scripts: Dict[str, bytes] = {}
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        if "__pycache__" in path.parts:
            continue
        try:
            scripts[path.relative_to(directory).as_posix()] = path.read_bytes()
        except OSError as e:
            raise EvaError(Code.CONFIG_INVALID, "读不到 skill 脚本", path=str(path), error=str(e)) from e
    if sum(len(b) for b in scripts.values()) > cfg.max_scripts_bytes:
        raise EvaError(Code.CONFIG_INVALID, "skill 脚本总量超过上限", directory=str(directory), max_bytes=cfg.max_scripts_bytes)
    return scripts


def load_skills(root: Path = ROOT, cfg: Optional[SkillsConfig] = None) -> Skills:
    cfg = cfg or load_config()
    if not Path(root).is_dir():
        raise EvaError(Code.CONFIG_INVALID, "skill 目录不存在", path=str(root))
    return Skills([_read_skill(d, cfg) for d in sorted(p for p in Path(root).iterdir() if p.is_dir())])


Fingerprint = Tuple[Tuple[str, int, int], ...]


def _fingerprint(root: Path) -> Fingerprint:
    """skills/ 下所有文件的（相对路径，mtime，大小）。只看文件：空目录不算 skill，创建过程中的半成品目录不触发重新加载。"""
    try:
        entries = []
        for p in sorted(Path(root).rglob("*")):
            if "__pycache__" in p.parts or not p.is_file():
                continue
            st = p.stat()
            entries.append((p.relative_to(root).as_posix(), st.st_mtime_ns, st.st_size))
        return tuple(entries)
    except OSError as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到 skill 目录", path=str(root), error=str(e)) from e


class SkillRegistry:
    """进程内持有当前的全局 skill 快照。构造时全部加载并校验，不合格抛 CONFIG_INVALID（此时没有上一份可保留）。

    current() 按 check_interval_seconds 比对文件指纹，变了就重新加载。新内容不合格或目录读不到时保留上一份合格的，
    同一版坏内容只报一次错误，等文件再变才重试。"""

    def __init__(self, root: Path = ROOT, cfg: Optional[SkillsConfig] = None):
        self._root, self._cfg = Path(root), cfg or load_config()
        self._lock = threading.Lock()
        self._print = _fingerprint(self._root)
        self._skills = load_skills(self._root, self._cfg)
        self._checked = clock.unix()
        self._failed: Optional[Fingerprint] = None

    @property
    def config(self) -> SkillsConfig:
        return self._cfg

    def current(self) -> Skills:
        with self._lock:
            now = clock.unix()
            if now - self._checked >= self._cfg.check_interval_seconds:
                self._checked = now
                self._reload_if_changed()
            return self._skills

    def _reload_if_changed(self) -> None:
        try:
            current = _fingerprint(self._root)
        except EvaError as e:
            logs.report_error(log, e, kept="上一份合格的 skill")
            return
        if current in (self._print, self._failed):
            return
        try:
            skills = load_skills(self._root, self._cfg)
        except EvaError as e:
            self._failed = current
            logs.report_error(log, e, kept="上一份合格的 skill")
            return
        self._skills, self._print, self._failed = skills, current, None
        logs.emit(log, logging.INFO, "skill 已重新加载", skills=skills.names())


ADDENDUM_TITLE = "本租户补充（只对当前用户生效，与上文冲突时以这里为准）"


def overlay(base: Skills, rows: List[dict], reported: Set[Tuple[str, int]]) -> Skills:
    """把一个租户生效的 skill 叠在公共层上：补充接在同名公共 skill 的正文后面，租户独有的并入清单。
    与公共层对不上的行（补充对应的公共 skill 没了，独有的名字被公共层占了）不叠，按 CONFIG_INVALID 报一次，其余照常。"""
    merged = dict(base._by_name)
    for r in rows:
        name, kind, in_base = r["name"], r["kind"], r["name"] in base._by_name
        if kind == SkillKind.ADDENDUM.value and in_base:
            g = merged[name]
            merged[name] = replace(g, body=f"{g.body}\n\n---\n{ADDENDUM_TITLE}：\n{r['body']}")
        elif kind == SkillKind.OWN.value and not in_base:
            merged[name] = Skill(name, r["description"], r["body"], {}, {})
        elif (name, r["version"]) not in reported:
            reported.add((name, r["version"]))
            logs.report_error(log, EvaError(Code.CONFIG_INVALID, "租户 skill 与公共层对不上，已忽略",
                                            name=name, kind=kind, version=r["version"]), ignored=True)
    return Skills(list(merged.values()))


class SkillView:
    """一个对话看到的 skill，接口同 Skills：公共层加上该租户生效的补充和独有 skill。

    refresh() 在每个 turn 开头调一次：公共层快照变了，或租户的修订号变了才重新叠加；到下次 refresh 之前，
    清单、正文和脚本都来自同一份快照，一个 turn 内不会前后不一致。
    tenant 是租户层的读取端（rev() 和 snapshot()，见 eva/storage/tenant_skills.py），不给就只有公共层。"""

    def __init__(self, registry: SkillRegistry, tenant: Optional[Any] = None):
        self._registry, self._tenant = registry, tenant
        self._base = self._skills = registry.current()
        self._rev: Optional[int] = None
        self._rows: List[dict] = []
        self._reported: Set[Tuple[str, int]] = set()

    def refresh(self) -> None:
        base = self._registry.current()
        rev = self._tenant.rev() if self._tenant else None
        if base is self._base and rev == self._rev:
            return
        if rev != self._rev:
            self._rev, self._rows = self._tenant.snapshot()
        self._base = base
        self._skills = overlay(base, self._rows, self._reported) if self._rows else base

    def __bool__(self) -> bool:
        return bool(self._skills)

    def catalog(self) -> str:
        return self._skills.catalog()

    def get(self, name: str) -> Skill:
        return self._skills.get(name)

    def reference(self, name: str, file: str) -> str:
        return self._skills.reference(name, file)

    def script_files(self) -> Dict[str, bytes]:
        return self._skills.script_files()