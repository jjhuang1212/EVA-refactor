"""configs/hooks.yaml 的加载与校验。"""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

import yaml

from eva.contracts.errors import Code, EvaError

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "configs" / "hooks.yaml"


@dataclass(frozen=True)
class CitationConfig:
    min_quote_chars: int
    max_reported: int
    uncited_min_chars: int = 0  # 回答至少多长，才在「用了查询结果却没引用」时拦一次
    uncited_tools: tuple = ()  # 本 turn 调用过其中任一个（且没报错），回答又没有任何引用，就拦一次；空表示不拦
    uncited_exempt_tools: tuple = ()  # 本 turn 调用过其中任一个（且没报错）的回答不查"没引用"：回答只是交付说明，事实在交付的文件里
    uncited_exempt_files: tuple = ()  # 同上，判据是本 turn 成功交付过文件名匹配其中任一个正则的文件（修订录音记录的 turn 不经过导出转写稿）


def load_citation(path: Path = DEFAULT_PATH) -> CitationConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了 hook 配置", path=str(path), error=str(e)) from e
    section = raw.get("citation") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        raise EvaError(Code.CONFIG_INVALID, "hook 配置需要 citation 小节", path=str(path))
    values = {}
    for key in ("min_quote_chars", "max_reported"):
        v = section.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise EvaError(Code.CONFIG_INVALID, f"citation.{key} 必须是正整数", value=repr(v))
        values[key] = v
    tools = section.get("uncited_tools", [])
    if not isinstance(tools, list) or not all(isinstance(t, str) and t for t in tools):
        raise EvaError(Code.CONFIG_INVALID, "citation.uncited_tools 必须是工具名列表", value=repr(tools))
    minimum = section.get("uncited_min_chars", 0)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
        raise EvaError(Code.CONFIG_INVALID, "citation.uncited_min_chars 必须是非负整数", value=repr(minimum))
    exempt = section.get("uncited_exempt_tools", [])
    if not isinstance(exempt, list) or not all(isinstance(t, str) and t for t in exempt):
        raise EvaError(Code.CONFIG_INVALID, "citation.uncited_exempt_tools 必须是工具名列表", value=repr(exempt))
    files = section.get("uncited_exempt_files", [])
    if not isinstance(files, list) or not all(isinstance(x, str) and x for x in files):
        raise EvaError(Code.CONFIG_INVALID, "citation.uncited_exempt_files 必须是正则列表", value=repr(files))
    for x in files:
        try:
            re.compile(x)
        except re.error as e:
            raise EvaError(Code.CONFIG_INVALID, "citation.uncited_exempt_files 里有不合法的正则", value=x, error=str(e)) from e
    return CitationConfig(**values, uncited_min_chars=minimum, uncited_tools=tuple(tools), uncited_exempt_tools=tuple(exempt),
                          uncited_exempt_files=tuple(files))


@dataclass(frozen=True)
class UserOnlyConfig:
    tools: tuple  # 只能由用户本轮说的话触发的工具
    safe_tools: tuple  # 本轮调用过这些之外的工具（没报错），就拒绝上面的工具
    min_overlap: float  # 带 text 参数的，内容的字至少这个比例出现在用户本轮说的话里


def load_user_only(path: Path = DEFAULT_PATH) -> UserOnlyConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了 hook 配置", path=str(path), error=str(e)) from e
    section = raw.get("user_only") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        raise EvaError(Code.CONFIG_INVALID, "hook 配置需要 user_only 小节", path=str(path))
    lists = {}
    for key in ("tools", "safe_tools"):
        v = section.get(key)
        if not isinstance(v, list) or not v or not all(isinstance(t, str) and t for t in v):
            raise EvaError(Code.CONFIG_INVALID, f"user_only.{key} 必须是非空的工具名列表", value=repr(v))
        lists[key] = tuple(v)
    overlap = section.get("min_overlap")
    if isinstance(overlap, bool) or not isinstance(overlap, (int, float)) or not 0 < overlap <= 1:
        raise EvaError(Code.CONFIG_INVALID, "user_only.min_overlap 必须在 0 到 1 之间（不含 0）", value=repr(overlap))
    return UserOnlyConfig(lists["tools"], lists["safe_tools"], float(overlap))


def check_user_only(cfg: UserOnlyConfig, registered) -> None:
    """名单里写错的工具名会让限制静默不生效或把正常调用挡住，装配时就抛错。"""
    unknown = sorted((set(cfg.tools) | set(cfg.safe_tools)) - set(registered))
    if unknown:
        raise EvaError(Code.CONFIG_INVALID, "user_only 里有未注册的工具", tools=unknown)


@dataclass(frozen=True)
class DeliveryGate:
    entered_by: str  # 本 turn 成功调用过它，说明已进入这条流程
    must_deliver: tuple  # 进入后，本 turn 对其中每个正则，都要成功交付过一个文件名匹配它的文件才允许结束
    max_blocks: int
    message: str
    skill: str = ""  # 非空时，进入流程后要先 load_skill 它，才能 deliver_file（防止模型手写文件交付）
    must_save: tuple = ()  # 进入后，本 turn 对其中每个正则，都要成功 write_file 过一个文件名匹配它的文件才允许结束（如后续修订要用的修订单）


def load_delivery_gates(path: Path = DEFAULT_PATH) -> Dict[str, DeliveryGate]:
    """交付门禁：流程名 -> 门禁。没有这一节表示不设门禁。"""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了 hook 配置", path=str(path), error=str(e)) from e
    section = raw.get("delivery_gates", {}) if isinstance(raw, dict) else {}
    if not isinstance(section, dict):
        raise EvaError(Code.CONFIG_INVALID, "delivery_gates 必须是映射", value=repr(section))
    out = {}
    for name, body in section.items():
        ok = (isinstance(body, dict) and all(isinstance(body.get(k), str) and body[k].strip() for k in ("entered_by", "message"))
              and isinstance(body.get("max_blocks"), int) and not isinstance(body["max_blocks"], bool) and body["max_blocks"] >= 1
              and isinstance(body.get("must_deliver"), list) and body["must_deliver"]
              and all(isinstance(x, str) and x for x in body["must_deliver"]))
        if not ok:
            raise EvaError(Code.CONFIG_INVALID, "delivery_gates.<流程> 需要 entered_by、message（非空字符串）、must_deliver（正则列表）和 max_blocks（正整数）", gate=repr(name))
        try:
            for x in body["must_deliver"]:
                re.compile(x)
        except re.error as e:
            raise EvaError(Code.CONFIG_INVALID, "delivery_gates.<流程>.must_deliver 里有不合法的正则", gate=repr(name), error=str(e)) from e
        skill, saves = body.get("skill", ""), body.get("must_save", [])
        if not isinstance(skill, str) or not isinstance(saves, list) or not all(isinstance(x, str) and x for x in saves):
            raise EvaError(Code.CONFIG_INVALID, "delivery_gates.<流程>.skill 必须是字符串，must_save 必须是正则列表", gate=repr(name))
        try:
            for x in saves:
                re.compile(x)
        except re.error as e:
            raise EvaError(Code.CONFIG_INVALID, "delivery_gates.<流程>.must_save 里有不合法的正则", gate=repr(name), error=str(e)) from e
        out[name] = DeliveryGate(body["entered_by"], tuple(body["must_deliver"]), body["max_blocks"], body["message"], skill.strip(), tuple(saves))
    return out


def load_scenarios(path: Path = DEFAULT_PATH) -> Dict[str, Tuple[str, ...]]:
    """场景名 -> 必备维度。声明了场景的报告交付前，这些维度都要查过（或说明为什么没查）。"""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了 hook 配置", path=str(path), error=str(e)) from e
    section = raw.get("scenarios") if isinstance(raw, dict) else None
    if not isinstance(section, dict) or not section:
        raise EvaError(Code.CONFIG_INVALID, "hook 配置需要非空的 scenarios 小节", path=str(path))
    out = {}
    for name, body in section.items():
        dims = body.get("required_dimensions") if isinstance(body, dict) else None
        if not isinstance(dims, list) or not dims or not all(isinstance(d, str) and re.fullmatch(r"[a-z][a-z_]*", d) for d in dims) \
                or len(set(dims)) != len(dims):
            raise EvaError(Code.CONFIG_INVALID, "scenarios.<场景>.required_dimensions 必须是不重复的维度名列表", scenario=repr(name))
        out[name] = tuple(dims)
    return out
