"""老式 doc/xls/ppt：用 soffice 转成同族的 OOXML 再解析。soffice 在解析镜像里，宿主不装。"""
import io
import shutil
import subprocess
import tempfile
from pathlib import Path

from eva.contracts.errors import Code, EvaError

TARGETS = {"doc": "docx", "xls": "xlsx", "ppt": "pptx"}
_STREAMS = {"doc": ("WordDocument",), "xls": ("Workbook", "Book"), "ppt": ("PowerPoint Document",)}
_TIMEOUT_SECONDS = 100  # 小于解析沙箱的总时限，超时由这里报转换失败，而不是整次运行被杀


def _check_container(data: bytes, ext: str) -> None:
    """soffice 对内容损坏的文件会转出一个空文档而不报错，所以先确认 OLE2 容器里有该格式的主数据流。"""
    import olefile

    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except OSError:  # olefile 对非 OLE2 或结构损坏的数据统一抛 OSError
        raise EvaError(Code.FILE_UNSUPPORTED, "老式 Office 文件已损坏，不是有效的 OLE2 容器", reason="corrupt",
                       format=ext) from None
    with ole:
        if not any(ole.exists(name) for name in _STREAMS[ext]):
            raise EvaError(Code.FILE_UNSUPPORTED, "文件内容与扩展名不符或已损坏", reason="corrupt", format=ext)


def convert(data: bytes, ext: str) -> bytes:
    _check_container(data, ext)
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice is None:
        raise EvaError(Code.SANDBOX_UNAVAILABLE, "解析环境里没有 soffice，老式 Office 文件无法转换", format=ext)
    target = TARGETS[ext]
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / f"source.{ext}"
        source.write_bytes(data)
        cmd = [soffice, "--headless", "--norestore", "--nolockcheck", f"-env:UserInstallation={(Path(tmp) / 'lo').as_uri()}",
               "--convert-to", target, "--outdir", tmp, str(source)]
        try:
            done = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise EvaError(Code.FILE_UNSUPPORTED, "老式 Office 文件转换超时", reason="convert_timeout", format=ext) from None
        result = Path(tmp) / f"source.{target}"
        if not result.is_file():
            raise EvaError(Code.FILE_UNSUPPORTED, "老式 Office 文件转换失败，文件可能已损坏或有密码", reason="corrupt",
                           format=ext, stderr=done.stderr[-300:].decode("utf-8", errors="replace"))
        return result.read_bytes()
