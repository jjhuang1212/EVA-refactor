"""运行用到的输入快照：configs/ 和 skills/ 下的全部文件（系统提示词也在 configs/ 里）。

内容相同就是同一版：版本号 inputs_sha 是所有文件路径和内容的哈希，快照按它存一份（out/_inputs/<sha>/），
各次运行只记这个哈希。运行前后各取一次，不一致说明运行期间有人改了文件，该次结果不可比。
"""
import difflib
import hashlib
import io
import shutil
import zipfile
from pathlib import Path
from typing import Dict, List

from eva.contracts.errors import Code, EvaError

ROOT = Path(__file__).resolve().parents[2]
DIRS = ("configs", "skills")


def collect(root: Path = ROOT) -> Dict[str, bytes]:
    """相对路径（正斜杠）到文件内容。__pycache__ 是运行产生的，不算输入。"""
    files = {}
    for d in DIRS:
        for p in sorted((root / d).rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                files[p.relative_to(root).as_posix()] = p.read_bytes()
    return files


def sha_of(files: Dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(path.encode("utf-8") + b"\0" + hashlib.sha256(files[path]).digest())
    return h.hexdigest()


def save(files: Dict[str, bytes], store: Path) -> str:
    """按哈希存一份快照，已有则不重写。返回哈希。"""
    sha = sha_of(files)
    target = store / sha
    if not target.exists():
        tmp = store / f".tmp-{sha}"
        for path, data in files.items():
            (tmp / path).parent.mkdir(parents=True, exist_ok=True)
            (tmp / path).write_bytes(data)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            tmp.rename(target)  # 写完再改名，不留写一半的快照
        except FileExistsError:  # 另一个进程刚存了同一版，内容相同
            shutil.rmtree(tmp)
    return sha


def load(sha: str, store: Path) -> Dict[str, bytes]:
    d = store / sha
    if not sha.isalnum() or not d.is_dir():
        raise EvaError(Code.FILE_NOT_FOUND, "没有这一版输入的快照", inputs_sha=sha)
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def to_zip(files: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(files):
            z.writestr(path, files[path])
    return buf.getvalue()


def from_zip(data: bytes) -> Dict[str, bytes]:
    """解开 to_zip 打的包。路径里有 .. 或绝对路径就拒绝，不让解出去的文件逃出目标目录。"""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        for n in names:
            if n.startswith("/") or ".." in Path(n).parts or ":" in n:
                raise EvaError(Code.FILE_UNSUPPORTED, "快照包里有不安全的路径", path=n)
        return {n: z.read(n) for n in names}


def diff(a: Dict[str, bytes], b: Dict[str, bytes]) -> List[dict]:
    """两版输入的差异：新增、删除、改动的文件，改动的带逐行 diff。二进制或非 UTF-8 的只标"已变"，不猜内容。"""
    out = []
    for path in sorted(set(a) | set(b)):
        if a.get(path) == b.get(path):
            continue
        item = {"path": path, "status": "added" if path not in a else "removed" if path not in b else "changed"}
        try:
            old = a.get(path, b"").decode("utf-8").splitlines(keepends=True)
            new = b.get(path, b"").decode("utf-8").splitlines(keepends=True)
            item["diff"] = list(difflib.unified_diff(old, new, f"A/{path}", f"B/{path}", n=2))
        except UnicodeDecodeError:
            item["diff"] = None
        out.append(item)
    return out
