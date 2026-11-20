"""OCR：调 tesseract 把一张图识别成文字和平均置信度。只在解析沙箱里用，tesseract 在解析镜像里，宿主不装。

Reader 管一份文件里所有图片的 OCR：共用一份张数额度、相同的图只识别一次，没读到的图按原因记下来，最后变成警告。
文档内嵌图，以及 OCR 没读出文字或置信度很低的图（照片、图表、流程图），如果开了看图，在文档里留一个占位块，图存进 images，交给宿主去问视觉模型；
沙箱里没有网络，也不持凭证，不在这里调模型。
"""
import hashlib
import io
import os
import re
import shutil
import subprocess
from typing import Callable, Dict, List, Optional, Tuple, Union

from eva.contracts.document import ParseLimits
from eva.parsing.builder import Builder
from eva.contracts.errors import Code, EvaError

_TIMEOUT_SECONDS = 60  # 单张图的时限，小于解析沙箱的总时限
_CJK = "⺀-﫿＀-￯"
_CJK_GAP = re.compile(f"(?<=[{_CJK}]) (?=[{_CJK}])")  # tesseract 会在汉字之间插空格


def recognize(png: bytes, languages: str) -> Tuple[str, int]:
    """返回 (文字, 词级置信度的平均值 0-100)。没有识别出任何文字时返回 ("", 0)，不是错误。"""
    exe = shutil.which("tesseract")
    if exe is None:
        raise EvaError(Code.SANDBOX_UNAVAILABLE, "解析环境里没有 tesseract，无法 OCR")
    try:
        done = subprocess.run([exe, "stdin", "stdout", "-l", languages, "--psm", "3", "tsv"], input=png,
                              capture_output=True, timeout=_TIMEOUT_SECONDS,
                              env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    except subprocess.TimeoutExpired:
        raise EvaError(Code.FILE_UNSUPPORTED, "OCR 超时", reason="ocr_timeout") from None
    if done.returncode != 0:
        raise EvaError(Code.FILE_UNSUPPORTED, "OCR 失败", reason="ocr_failed", languages=languages,
                       stderr=done.stderr[-300:].decode("utf-8", errors="replace"))
    lines, confs = {}, []
    for row in done.stdout.decode("utf-8", errors="replace").splitlines()[1:]:
        cols = row.split("\t", 11)
        if len(cols) == 12 and cols[0] == "5" and cols[11].strip() and float(cols[10]) >= 0:
            lines.setdefault(tuple(cols[2:5]), []).append(cols[11].strip())
            confs.append(float(cols[10]))
    text = "\n".join(_CJK_GAP.sub("", " ".join(words)) for words in lines.values())
    return text, round(sum(confs) / len(confs)) if confs else 0


def load(data: bytes):
    """按 EXIF 转正并统一成 RGB。不是图片或已损坏时抛 PIL 的异常。"""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as raw:
        return ImageOps.exif_transpose(raw).convert("RGB")


def _png(image) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _unreadable_errors() -> tuple:
    from PIL import Image
    return OSError, ValueError, SyntaxError, Image.DecompressionBombError  # emf/wmf/svg、损坏的内嵌图都落在这几类


def _blank(image, threshold: float) -> bool:
    from PIL import ImageStat
    return ImageStat.Stat(image.convert("L")).stddev[0] < threshold


def _fit(image, max_bytes: int) -> bytes:
    """编码成 png；超过 max_bytes 就按比例缩小到够小为止。"""
    while True:
        png = _png(image)
        if len(png) <= max_bytes or min(image.size) <= 64:
            return png
        image = image.resize((int(image.width * 0.75), int(image.height * 0.75)))


class Reader:
    _WHERE_SHOWN = 20  # 警告里最多列几处位置，总数另给

    def __init__(self, limits: ParseLimits):
        self._limits = limits
        self._seen: Dict[str, Tuple[str, int]] = {}
        self._used = 0
        self._skipped: List[str] = []
        self._empty: List[str] = []
        self._unreadable: List[str] = []
        self._small = 0
        self._blank = 0
        self._vision_skipped: List[str] = []
        self._to_vision_no_text: List[str] = []
        self._to_vision_low: List[str] = []
        self._candidate: Optional[bytes] = None
        self.images: Dict[int, bytes] = {}  # 占位块的块序号 → 留给视觉模型的图（png）

    def read(self, source: Union[Callable, object], where: str, *, embedded: bool = False,
             size: Optional[Tuple[int, int]] = None) -> Optional[Tuple[str, int]]:
        """source 是 PIL 图，或返回 PIL 图的函数（额度用完时不调用，省掉渲染）。where 是警告里给人看的位置。
        embedded 表示文档里嵌入的图：打不开的记为"读不了"，小于 ocr_min_image_pixels 的不识别；
        size 是调用方已知的原始像素尺寸，给了就能在渲染之前过滤。非嵌入的图（图片文件、扫描页）打不开直接抛错。
        返回 (文字, 置信度)；没有读到文字返回 None，原因已记下。"""
        if embedded and size is not None and min(size) < self._limits.ocr_min_image_pixels:
            self._small += 1
            return None
        if self._used >= self._limits.ocr_max_images:
            self._skipped.append(where)
            return None
        try:
            image = source() if callable(source) else source
        except _unreadable_errors():
            if not embedded:
                raise
            self._unreadable.append(where)
            return None
        if embedded and size is None and min(image.size) < self._limits.ocr_min_image_pixels:
            self._small += 1
            return None
        png = _png(image)
        key = hashlib.sha256(png).hexdigest()
        if key not in self._seen:
            self._used += 1
            self._seen[key] = recognize(png, self._limits.ocr_languages)
        text, confidence = self._seen[key]
        if not text.strip():
            self._no_text(image, where)
            return None
        if embedded:  # 有标签的流程图 OCR 也读得出字，但读不出关系，所以文档里的内嵌图读到字也照样留给视觉模型
            self._offer(image, where)
        elif confidence < self._limits.vision_low_confidence and self._offer(image, where):
            self._to_vision_low.append(f"{where}（置信度 {confidence}）")
            return None  # 置信度太低，读出来的多半是乱码，只给视觉模型看
        return text, confidence

    def _no_text(self, image, where: str) -> None:
        """没读出文字：开了看图的话，空白图丢掉，其余的留给视觉模型；额度用完的记下来。"""
        if self._limits.vision_max_images == 0:
            self._empty.append(where)
        elif _blank(image, self._limits.vision_blank_stddev):
            self._blank += 1
        elif len(self.images) >= self._limits.vision_max_images:
            self._empty.append(where)
            self._vision_skipped.append(where)
        else:
            self._candidate = _fit(image, self._limits.vision_max_image_bytes)
            self._to_vision_no_text.append(where)

    def _offer(self, image, where: str) -> bool:
        """OCR 已读出文字的图：额度内留给视觉模型并返回 True；额度满了只记一笔，文字已经读到了。"""
        if self._limits.vision_max_images == 0:
            return False
        if len(self.images) >= self._limits.vision_max_images:
            self._vision_skipped.append(where)
            return False
        self._candidate = _fit(image, self._limits.vision_max_image_bytes)
        return True

    def read_into(self, builder: Builder, source: Union[Callable, object], anchor: Dict, where: str, *,
                  embedded: bool = False, size: Optional[Tuple[int, int]] = None) -> bool:
        """read 的结果直接成块：有文字成 OCR 块；留给了视觉模型的图再跟一个占位块（内嵌图读到字也留）。返回是否成了块。"""
        self._candidate = None
        result = self.read(source, where, embedded=embedded, size=size)
        if result:
            builder.ocr(result, anchor)
        if self._candidate is None:
            return bool(result)
        self.images[builder.vision(anchor)] = self._candidate
        return True

    def unreadable(self, where: str) -> None:
        """调用方一开始就拿不到图片内容（外链图片、缺失的部件）。"""
        self._unreadable.append(where)

    def warnings(self) -> Tuple[dict, ...]:
        found = [{"reason": reason, "count": len(items), "where": items[:self._WHERE_SHOWN]}
                 for reason, items in (("ocr_skipped", self._skipped), ("ocr_found_no_text", self._empty),
                                       ("images_unreadable", self._unreadable)) if items]
        for reason, items in (("vision_no_text", self._to_vision_no_text), ("vision_low_confidence", self._to_vision_low)):
            if items:
                found.append({"reason": reason, "count": len(items), "where": items[:self._WHERE_SHOWN]})
        if self._small:
            found.append({"reason": "small_images_ignored", "count": self._small})
        if self._blank:
            found.append({"reason": "blank_images_ignored", "count": self._blank})
        if self._vision_skipped:
            found.append({"reason": "vision_skipped", "count": len(self._vision_skipped),
                          "where": self._vision_skipped[:self._WHERE_SHOWN]})
        return tuple(found)
