"""开发机后端：宿主子进程。只有超时、输出上限、环境变量白名单，不隔离网络和文件系统，所以报告 partial。"""
import os
import sys
import threading
from pathlib import Path
from typing import Tuple

from eva.sandbox.base import META_DIR, Sandbox
from eva.sandbox.process import run_capped

# 凭证（企微 secret、模型 key、数据库口令）都在父进程环境里，子进程只拿运行解释器必需的几项。
# SYSTEMDRIVE 必须有：Windows 字体缓存的路径是 %SystemDrive%\ProgramData\...，缺了就会在工作目录里建一个字面叫 %SystemDrive% 的目录，产物校验整个拒收
_ENV_KEEP = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP")


class LocalSandbox(Sandbox):
    enforcement = "partial"

    def _execute(self, workdir: Path, cancel: threading.Event) -> Tuple[int, bytes, bytes]:
        env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
        env.update(PYTHONPATH=str(workdir), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
                   HOME=str(workdir / META_DIR), MPLCONFIGDIR=str(workdir / META_DIR))
        return run_capped([sys.executable, "-u", "-B", f"{META_DIR}/run.py"], workdir, env,
                          self._cfg.timeout_seconds, self._cfg.max_output_bytes, cancel)
