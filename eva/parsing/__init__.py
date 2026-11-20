"""文件解析：字节 → 有序的块。只在沙箱（解析镜像）里运行，只依赖 contracts；第三方库在各格式模块里按需导入。

格式靠内容识别，不全信扩展名：zip 容器看里面的目录，OLE2 容器按扩展名族判断。
"""
import importlib
import io
import posixpath
import zipfile
from typing import Dict, Tuple

from eva.contracts.document import Parsed, ParseLimits
from eva.contracts.errors import Code, EvaError

SUPPORTED = (".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf", ".md", ".png", ".jpg", ".jpeg")
_MODULES = {"docx": "docx", "xlsx": "xlsx", "pptx": "pptx", "pdf": "pdf", "md": "markdown", "png": "image", "jpg": "image"}
_IMAGE_MAGIC = {"png": b"\x89PNG\r\n\x1a\n", "jpg": b"\xff\xd8\xff"}
_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MARKERS = (("word/document.xml", "docx"), ("xl/workbook.xml", "xlsx"), ("ppt/presentation.xml", "pptx"))
_LEGACY_OF = {"docx": "doc", "xlsx": "xls", "pptx": "ppt"}


def _bad(reason: str, message: str, **details) -> EvaError:
    return EvaError(Code.FILE_UNSUPPORTED, message, reason=reason, **details)


def detect(filename: str, data: bytes) -> str:
    ext = posixpath.splitext(filename)[1].lower()
    if ext not in SUPPORTED:
        raise _bad("type", "不支持的文件类型", extension=ext, supported=list(SUPPORTED))
    fmt = ext[1:]
    if fmt == "md":
        return fmt
    if fmt in ("png", "jpg", "jpeg"):
        for kind, magic in _IMAGE_MAGIC.items():
            if data.startswith(magic):
                return kind
        raise _bad("corrupt", "文件内容不是 PNG 或 JPEG 图片", extension=ext)
    if fmt == "pdf":
        if b"%PDF" not in data[:1024]:
            raise _bad("corrupt", "文件不是 PDF", extension=ext)
        return fmt
    if data.startswith(_OLE2):
        return fmt if fmt in ("doc", "xls", "ppt") else _LEGACY_OF[fmt]
    if data.startswith(b"PK"):
        try:
            names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        except zipfile.BadZipFile:
            raise _bad("corrupt", "文件损坏，不是有效的压缩容器", extension=ext) from None
        for marker, kind in _ZIP_MARKERS:
            if marker in names:
                return kind
    raise _bad("corrupt", "文件内容与扩展名不符或已损坏", extension=ext)


def parse_with_images(filename: str, data: bytes, limits: ParseLimits, version: str) -> Tuple[Parsed, Dict[int, bytes]]:
    """第二项是留给视觉模型看的图（占位块的块序号 → png）；占位块要等宿主拿到识别结果后才算读完。"""
    fmt = detect(filename, data)
    shown = fmt
    if fmt in ("doc", "xls", "ppt"):
        from eva.parsing import legacy
        data, fmt = legacy.convert(data, fmt), legacy.TARGETS[fmt]
    blocks, warnings, images = importlib.import_module(f"eva.parsing.{_MODULES[fmt]}").parse(data, limits)
    return Parsed(shown, version, blocks, warnings), images


def parse(filename: str, data: bytes, limits: ParseLimits, version: str) -> Parsed:
    return parse_with_images(filename, data, limits, version)[0]
