"""评测案例：一个目录，里面是 case.yaml 和它引用的样本文件。

case.yaml：
  message: 用户发的话
  files: [上传的文件，相对案例目录，按顺序上传]      # 可省略
  persona: 补充信息，模型扮演用户时据此回答（用户是谁、手上有什么事实）    # 可省略
  asserts:                                          # 可省略，不写就只记录结果不判定
    reply_contains: [最终回复里必须出现的词]
    reply_not_contains: [最终回复里不能出现的词]
    max_steps: 40
    max_cost_cny: 1.0
    max_questions: 2           # agent 中途最多可以向用户提问几次；写 0 表示不该提问
agent 中途提问时怎么办不写在案例里，由运行时选：不交互（这次运行就此停下，判不通过）、人工回答，或模型扮演用户（据 persona 回答）。
网页端以后新建案例，就是写出同样的目录。格式不对一律抛 E0003，不猜。
"""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import List, Optional

import yaml

from eva.contracts.errors import Code, EvaError

CASES = Path(__file__).resolve().parent / "cases"
_ASSERTS = ("reply_contains", "reply_not_contains", "max_steps", "max_cost_cny", "max_questions")


@dataclass(frozen=True)
class Case:
    name: str
    dir: Path
    message: str
    files: List[Path]
    reply_contains: List[str]
    max_steps: Optional[int]
    max_cost_cny: Optional[Decimal]
    reply_not_contains: List[str] = field(default_factory=list)
    max_questions: Optional[int] = None
    persona: Optional[str] = None


def _bad(path: Path, why: str, **details) -> EvaError:
    return EvaError(Code.CONFIG_INVALID, f"评测案例 {why}", path=str(path), **details)


def load(case_dir) -> Case:
    d = Path(case_dir).resolve()
    path = d / "case.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise _bad(path, "读不到或解析不了 case.yaml", error=str(e)) from e
    if not isinstance(raw, dict) or set(raw) - {"message", "files", "asserts", "persona"}:
        raise _bad(path, "只能有 message、files、asserts、persona 四项")
    if not isinstance(raw.get("message"), str) or not raw["message"].strip():
        raise _bad(path, "message 必须是非空字符串")
    persona = raw.get("persona")
    if persona is not None and (not isinstance(persona, str) or not persona.strip()):
        raise _bad(path, "persona 必须是非空字符串")
    names = raw.get("files", [])
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise _bad(path, "files 必须是字符串列表")
    files = [d / n for n in names]
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        raise _bad(path, "files 里有文件不存在", missing=missing)
    a = raw.get("asserts", {})
    if not isinstance(a, dict) or set(a) - set(_ASSERTS):
        raise _bad(path, f"asserts 只支持 {', '.join(_ASSERTS)}")
    words = a.get("reply_contains", [])
    if not isinstance(words, list) or not all(isinstance(w, str) and w for w in words):
        raise _bad(path, "reply_contains 必须是非空字符串列表")
    banned = a.get("reply_not_contains", [])
    if not isinstance(banned, list) or not all(isinstance(w, str) and w for w in banned):
        raise _bad(path, "reply_not_contains 必须是非空字符串列表")
    steps = a.get("max_steps")
    if steps is not None and (isinstance(steps, bool) or not isinstance(steps, int) or steps < 1):
        raise _bad(path, "max_steps 必须是正整数")
    questions = a.get("max_questions")
    if questions is not None and (isinstance(questions, bool) or not isinstance(questions, int) or questions < 0):
        raise _bad(path, "max_questions 必须是不小于 0 的整数")
    cost = a.get("max_cost_cny")
    try:
        cost = None if cost is None else Decimal(str(cost))
    except InvalidOperation as e:
        raise _bad(path, "max_cost_cny 必须是数字") from e
    if cost is not None and (not cost.is_finite() or cost <= 0):
        raise _bad(path, "max_cost_cny 必须是正数")
    return Case(d.name, d, raw["message"], files, words, steps, cost, banned, questions, persona)
