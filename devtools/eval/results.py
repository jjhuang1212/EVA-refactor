"""读评测运行结果（devtools/eval/out）。只读，没有 result.json 的目录（运行中途崩溃）不在内。网页和标注都从这里读。"""
import json
from collections import Counter
from pathlib import Path
from typing import List, Optional, Tuple

from devtools.eval.runner import NAME, OUT
from eva.contracts.errors import Code, EvaError

_KEEP = ("case", "run_id", "tenant_id", "session_id", "started_at", "passed", "reason", "steps")
_OPTIONAL = ("label", "code_version", "inputs_sha")  # 这几项是后加的，更早的运行没有，列表里记为空，不是缺陷


def _check_name(kind: str, value: str) -> None:
    if not NAME.fullmatch(value):
        raise EvaError(Code.CONFIG_INVALID, f"{kind}只能含字母、数字、_、-，1 到 40 个字符", **{kind: value})


def _summary(res: dict) -> dict:
    return {**{k: res[k] for k in _KEEP}, **{k: res.get(k) for k in _OPTIONAL}, "cost_cny": res["cost"]["total_cny"],
            "error_code": res["error"]["code"] if res["error"] else None}


def list_results(case: Optional[str], limit: int, out_root: Path = OUT, offset: int = 0) -> Tuple[List[dict], bool]:
    """新的在前。返回 (本页行, 是否还有下一页)。"""
    if case is not None:
        _check_name("case", case)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise EvaError(Code.INVALID_VALUE, "offset 必须是非负整数", offset=offset)
    pattern = f"{case or '*'}/*/result.json"
    files = sorted(out_root.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    rows = [_summary(json.loads(p.read_text(encoding="utf-8"))) for p in files[offset:offset + limit]]
    return rows, len(files) > offset + limit


def list_labels(case: str, out_root: Path = OUT) -> List[dict]:
    """某案例下所有出现过的标签及运行次数（扫全部结果，不受分页限制）。按次数多、名称升序排。"""
    _check_name("case", case)
    counts = Counter()
    for p in (out_root / case).glob("*/result.json"):
        label = json.loads(p.read_text(encoding="utf-8")).get("label")
        if label:
            counts[label] += 1
    return [{"label": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def run_dir(case: str, run_id: str, out_root: Path = OUT) -> Path:
    _check_name("case", case)
    _check_name("run_id", run_id)
    d = out_root / case / run_id
    if not (d / "result.json").is_file():
        raise EvaError(Code.FILE_NOT_FOUND, "没有这次运行的结果", case=case, run_id=run_id)
    return d


def read_result(case: str, run_id: str, out_root: Path = OUT) -> dict:
    return json.loads((run_dir(case, run_id, out_root) / "result.json").read_text(encoding="utf-8"))
