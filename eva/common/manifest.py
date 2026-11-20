"""报告文件里嵌的引用清单。组件库（skills/report）写进去，交付前的校验和交付记录从这里读，所以放在两边都能用的底层。"""
import io
import json
import zipfile
from typing import Optional, Tuple

MANIFEST_NAME = "eva-citations.json"  # 报告组件库把引用清单嵌在 PDF 里的附件名，与 skills/report/scripts/eva_report/pdf.py 一致
DOCX_MANIFEST_PART = "customXml/eva-citations.json"  # docx 里的位置，与 eva_report/word.py 的 MANIFEST_PART 一致


def manifest_bytes(data: bytes) -> Tuple[Optional[bytes], Optional[str]]:
    """从 PDF 的附件或 docx 的自定义部件里取出清单原文。其他文件、没带清单的：(None, None)。"""
    if data.startswith(b"%PDF"):
        from pypdf import PdfReader
        try:
            attachments = PdfReader(io.BytesIO(data)).attachments
        except Exception as e:  # noqa: BLE001 pypdf 对坏文件抛各种类型；这里转成明确的拒绝，不吞掉
            return None, f"这个 PDF 打不开（{type(e).__name__}），不能交付"
        return (attachments[MANIFEST_NAME][0], None) if MANIFEST_NAME in attachments else (None, None)
    if data.startswith(b"PK"):  # docx 是 zip；别的 zip、xlsx 没有这个部件，不算报告
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                return (z.read(DOCX_MANIFEST_PART), None) if DOCX_MANIFEST_PART in z.namelist() else (None, None)
        except zipfile.BadZipFile:
            return None, None
    return None, None


def _scenario_ok(s) -> bool:
    return s is None or (isinstance(s, dict) and isinstance(s.get("name"), str) and isinstance(s.get("uscc"), str)
                         and isinstance(s.get("gaps"), dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in s["gaps"].items()))


def read_manifest(data: bytes) -> Tuple[Optional[list], Optional[dict], Optional[str]]:
    """返回 (引用清单, 场景声明或 None, 不放行的原因)。不是报告或没有清单：(None, None, None)。"""
    raw, problem = manifest_bytes(data)
    if raw is None:
        return None, None, problem
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, None, "文件里的引用清单损坏，请重新用 save_pdf 或 save_docx 生成"
    entries = body.get("citations") if isinstance(body, dict) else None
    ok = isinstance(entries, list) and _scenario_ok(body.get("scenario")) and all(
        isinstance(e, dict) and isinstance(e.get("no"), int) and e.get("kind") in ("b", "L", "w", "q")
        and isinstance(e.get("file"), str) and isinstance(e.get("first"), int) and isinstance(e.get("last"), int)
        and isinstance(e.get("quote"), str) and (e["kind"] != "w" or isinstance(e.get("url"), str)) for e in entries)
    return (entries, body["scenario"], None) if ok else (None, None, "文件里的引用清单格式不对，请重新用 save_pdf 或 save_docx 生成")
