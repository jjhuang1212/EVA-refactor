"""人工标注和认可版本。

  python -m devtools.eval.marks annotate <案例> <run_id> pass|fail [--note 备注]
  python -m devtools.eval.marks mark-version <案例> <run_id> <版本名> [--note 备注]
  python -m devtools.eval.marks versions
  python -m devtools.eval.marks show-version <版本名> [--dir 目录]

数据在数据库（挂在评测租户下，只追加），认可版本的快照在对象存储里，运行结果仍在本地 out/。
标注行里带着租户、会话、输入版本和自动判定，本地结果没了，统计和回放仍然能做。
"""
import argparse
import sys
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

from devtools.eval import results, snapshot
from devtools.eval.runner import INPUTS_DIR, OUT
from eva.contracts.errors import Code, EvaError
from eva.storage import db
from eva.storage.developer import Developer
from eva.storage.eval_marks import EvalMarks
from eva.storage.objects import ObjectStore, Objects


def annotate(case: str, run_id: str, verdict: str, note: Optional[str] = None, out_root: Path = OUT) -> dict:
    """给一次运行标人工判定。运行结果里的租户、会话、输入版本、自动判定一起记下来。再标就是追加，最新一条生效。"""
    res = results.read_result(case, run_id, out_root)
    with db.transaction() as conn:
        seq = EvalMarks(conn, res["tenant_id"]).annotate(
            run_id, case, res["session_id"], res.get("inputs_sha"), res.get("label"), bool(res["passed"]), verdict, note)
    return {"run_id": run_id, "tenant_id": res["tenant_id"], "seq": seq, "verdict": verdict}


def mark_version(case: str, run_id: str, name: str, note: Optional[str] = None, out_root: Path = OUT) -> dict:
    """认可这次运行用的那一版输入：起名，并把快照存进对象存储。"""
    res = results.read_result(case, run_id, out_root)
    sha = res.get("inputs_sha")
    if sha is None:
        raise EvaError(Code.INVALID_VALUE, "这次运行没有记录输入版本，无法认可（早期运行）", run_id=run_id)
    files = snapshot.load(sha, out_root / INPUTS_DIR)
    with db.transaction() as conn:
        seq = EvalMarks(conn, res["tenant_id"]).mark_version(sha, name, note)
        Objects(conn, res["tenant_id"], ObjectStore.from_config()).put_eval_inputs(sha, snapshot.to_zip(files))
    return {"name": name, "inputs_sha": sha, "tenant_id": res["tenant_id"], "seq": seq, "files": len(files)}


def _stats(rows: List[dict]) -> dict:
    return {"runs": len(rows), "auto_passed": sum(r["auto_passed"] for r in rows),
            "human_pass": sum(r["verdict"] == "pass" for r in rows), "human_fail": sum(r["verdict"] == "fail" for r in rows)}


def list_versions() -> List[dict]:
    """所有认可过的版本，每个附带标注统计（来自数据库，不依赖本地 out/）。"""
    with db.transaction() as conn:
        dev = Developer(conn)
        return [{**v, **_stats(dev.annotations(inputs_sha=v["inputs_sha"]))} for v in dev.versions()]


def fetch_version(name: str) -> dict:
    """按名字取出认可版本：版本信息、全部文件内容、它对应的标注。同名不唯一或不存在都报错，不猜。"""
    with db.transaction() as conn:
        dev = Developer(conn)
        found = [v for v in dev.versions() if v["name"] == name]
        if not found:
            raise EvaError(Code.FILE_NOT_FOUND, "没有这个认可版本", name=name)
        if len(found) > 1:
            raise EvaError(Code.INVALID_VALUE, "有多个租户用了这个版本名，请改用不重名的名字", name=name,
                           tenants=[v["tenant_id"] for v in found])
        v = found[0]
        files = snapshot.from_zip(Objects(conn, v["tenant_id"], ObjectStore.from_config()).read_eval_inputs(v["inputs_sha"]))
        annotations = dev.annotations(inputs_sha=v["inputs_sha"])
    if snapshot.sha_of(files) != v["inputs_sha"]:
        raise EvaError(Code.STORAGE_ERROR, "存下的快照内容和版本号对不上", name=name, inputs_sha=v["inputs_sha"])
    return {"version": v, "files": files, "annotations": annotations, **_stats(annotations)}


def _write_files(files: dict, dest: Path) -> None:
    for path, data in files.items():
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m devtools.eval.marks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("annotate")
    a.add_argument("case"), a.add_argument("run_id"), a.add_argument("verdict", choices=["pass", "fail"])
    a.add_argument("--note")
    m = sub.add_parser("mark-version")
    m.add_argument("case"), m.add_argument("run_id"), m.add_argument("name")
    m.add_argument("--note")
    sub.add_parser("versions")
    s = sub.add_parser("show-version")
    s.add_argument("name")
    s.add_argument("--dir", type=Path, help="把这一版的文件写到这个目录（不会碰工作区的 configs/ 和 skills/）")
    args = ap.parse_args(argv)
    load_dotenv()
    try:
        if args.cmd == "annotate":
            print(annotate(args.case, args.run_id, args.verdict, args.note))
        elif args.cmd == "mark-version":
            print(mark_version(args.case, args.run_id, args.name, args.note))
        elif args.cmd == "versions":
            for v in list_versions():
                print(f"{v['name']}  {v['inputs_sha'][:8]}  租户 {v['tenant_id']}  标注运行 {v['runs']} 次  "
                      f"自动通过 {v['auto_passed']}  人工通过 {v['human_pass']}  人工不通过 {v['human_fail']}")
        else:
            got = fetch_version(args.name)
            v = got["version"]
            print(f"{v['name']}  {v['inputs_sha'][:8]}  租户 {v['tenant_id']}  文件 {len(got['files'])} 个  "
                  f"标注运行 {got['runs']} 次  自动通过 {got['auto_passed']}  人工通过 {got['human_pass']}  人工不通过 {got['human_fail']}")
            for r in got["annotations"]:
                print(f"  {r['case_name']} {r['run_id']}  自动{'通过' if r['auto_passed'] else '不通过'}  人工 {r['verdict']}  {r['note'] or ''}")
            if args.dir:
                _write_files(got["files"], args.dir)
                print(f"文件已写到 {args.dir}")
    except EvaError as e:
        print(f"{e} {e.details or ''}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
