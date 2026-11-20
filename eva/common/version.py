"""当前代码的 git 版本，写进模型调用记录，把一次运行对应到提示词和代码的版本。"""
import functools
import subprocess
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[2]


@functools.lru_cache(maxsize=None)
def code_version() -> Optional[str]:
    """形如 `c7aa16c` 或 `c7aa16c+dirty`（有未提交改动）。进程内只取一次；不在 git 仓库里返回 None，记录里就是明确的"未知"。"""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=_ROOT, capture_output=True, text=True, timeout=5,
                              check=True).stdout.strip()
    try:
        return git("rev-parse", "--short", "HEAD") + ("+dirty" if git("status", "--porcelain") else "")
    except (OSError, subprocess.SubprocessError):
        return None
