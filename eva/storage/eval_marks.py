"""eval_annotations 和 eval_input_versions：评测的人工标注和认可版本，只追加，挂在评测租户下。

同一运行（或同一输入版本）再标一次就加一行，序号最大的生效；历史行保留。校验在这里做，坏内容进不了库。
跨租户的统计读走开发者只读入口（developer.py），不走这里。
"""
import re
from typing import List, Optional

from eva.common import clock
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import EvalVerdict, TenantRealm, check
from eva.storage import tenants
from eva.storage.repo import TenantRepo

_ID = re.compile(r"[A-Za-z0-9_\-]{1,40}")
_SHA = re.compile(r"[0-9a-f]{64}")
ANNOTATION_COLS = ("tenant_id", "run_id", "seq", "case_name", "session_id", "inputs_sha", "label", "auto_passed",
                   "verdict", "note", "created_at")
VERSION_COLS = ("tenant_id", "inputs_sha", "seq", "name", "note", "created_at")
# 每个运行（或每个版本）序号最大的一行。开发者只读入口在不限租户时复用同一段
LATEST_ANNOTATION = ("SELECT {cols} FROM eval_annotations a WHERE seq = (SELECT MAX(seq) FROM eval_annotations "
                     "WHERE tenant_id = a.tenant_id AND run_id = a.run_id)")
LATEST_VERSION = ("SELECT {cols} FROM eval_input_versions v WHERE seq = (SELECT MAX(seq) FROM eval_input_versions "
                  "WHERE tenant_id = v.tenant_id AND inputs_sha = v.inputs_sha)")


def annotation_row(r) -> dict:
    d = dict(zip(ANNOTATION_COLS, r))
    d["auto_passed"] = bool(d["auto_passed"])
    return d


def version_row(r) -> dict:
    return dict(zip(VERSION_COLS, r))


def _need(kind: str, pattern: "re.Pattern", value, required: bool = True) -> Optional[str]:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise EvaError(Code.INVALID_VALUE, f"{kind} 格式不对", **{kind: repr(value)})
    return value


def _note(note) -> Optional[str]:
    if note is None:
        return None
    if not isinstance(note, str) or len(note) > 1024:
        raise EvaError(Code.INVALID_VALUE, "备注必须是不超过 1024 字的文字")
    return note


class EvalMarks(TenantRepo):
    def __init__(self, conn, tenant_id):
        super().__init__(conn, tenant_id)
        tenants.require_realm(self.tenant_id, TenantRealm.EVAL)  # 评测的标注只写在评测租户上，不碰真实用户、模拟器和终端的租户

    def annotate(self, run_id: str, case_name: str, session_id: Optional[str], inputs_sha: Optional[str],
                 label: Optional[str], auto_passed: bool, verdict: str, note: Optional[str]) -> int:
        """给一次运行标人工判定，返回这一行的序号。再标就是追加。"""
        verdict = check(EvalVerdict, verdict).value
        _need("run_id", _ID, run_id)
        _need("case_name", _ID, case_name)
        _need("session_id", _ID, session_id, required=False)
        _need("inputs_sha", _SHA, inputs_sha, required=False)
        _need("label", _ID, label, required=False)
        if not isinstance(auto_passed, bool):
            raise EvaError(Code.INVALID_VALUE, "auto_passed 必须是布尔值", value=repr(auto_passed))
        seq = self._next_seq("eval_annotations", "run_id", run_id)
        self._execute(
            "INSERT INTO eval_annotations (tenant_id, run_id, seq, case_name, session_id, inputs_sha, label, "
            "auto_passed, verdict, note, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (self.tenant_id, run_id, seq, case_name, session_id, inputs_sha, label, int(auto_passed), verdict,
             _note(note), clock.now()))
        return seq

    def annotations(self, run_id: str) -> List[dict]:
        """一次运行的全部标注历史，旧的在前。没标过返回空列表。"""
        rows = self._execute(f"SELECT {', '.join(ANNOTATION_COLS)} FROM eval_annotations "
                             "WHERE tenant_id = %s AND run_id = %s ORDER BY seq", (self.tenant_id, run_id)).fetchall()
        return [annotation_row(r) for r in rows]

    def latest_annotations(self, case_name: Optional[str] = None) -> List[dict]:
        """本租户每次运行当前生效的标注，可按案例筛。"""
        sql = LATEST_ANNOTATION.format(cols=", ".join("a." + c for c in ANNOTATION_COLS)) + " AND a.tenant_id = %s"
        params = [self.tenant_id]
        if case_name is not None:
            sql, params = sql + " AND a.case_name = %s", params + [_need("case_name", _ID, case_name)]
        return [annotation_row(r) for r in self._execute(sql + " ORDER BY a.created_at", tuple(params)).fetchall()]

    def mark_version(self, inputs_sha: str, name: str, note: Optional[str]) -> int:
        """给一个输入版本起名（认可），返回序号。名字在本租户内不能给另一个版本用；同一版本改名就是追加。"""
        _need("inputs_sha", _SHA, inputs_sha)
        if not isinstance(name, str) or not name.strip() or len(name) > 64 or name != name.strip():
            raise EvaError(Code.INVALID_VALUE, "版本名必须是 1 到 64 个字，首尾不能有空白", name=repr(name))
        clash = [v for v in self.versions() if v["name"] == name and v["inputs_sha"] != inputs_sha]
        if clash:
            raise EvaError(Code.INVALID_VALUE, "这个版本名已经给了另一个输入版本", name=name,
                           inputs_sha=clash[0]["inputs_sha"])
        seq = self._next_seq("eval_input_versions", "inputs_sha", inputs_sha)
        self._execute("INSERT INTO eval_input_versions (tenant_id, inputs_sha, seq, name, note, created_at) "
                      "VALUES (%s, %s, %s, %s, %s, %s)",
                      (self.tenant_id, inputs_sha, seq, name, _note(note), clock.now()))
        return seq

    def versions(self) -> List[dict]:
        """本租户认可过的输入版本，每个版本取当前生效的名字，旧的在前。"""
        sql = LATEST_VERSION.format(cols=", ".join("v." + c for c in VERSION_COLS)) + " AND v.tenant_id = %s ORDER BY v.created_at"
        return [version_row(r) for r in self._execute(sql, (self.tenant_id,)).fetchall()]

    def _next_seq(self, table: str, key: str, value: str) -> int:
        """表名和列名是本文件里写死的，不来自调用方。锁住这一段读，并发写同一对象时序号不重复。"""
        r = self._execute(f"SELECT ISNULL(MAX(seq), 0) + 1 FROM {table} WITH (UPDLOCK, HOLDLOCK) "
                          f"WHERE tenant_id = %s AND {key} = %s", (self.tenant_id, value)).fetchone()
        return r[0]
