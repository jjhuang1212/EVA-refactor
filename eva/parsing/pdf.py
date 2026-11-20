"""pdf：读文本层，一页至少一个块，出处是物理页码。没有文本层的页渲染后 OCR，OCR 不了的页明确标"未读"，不当成空页。

有文本层的页里的图片按位置裁出来 OCR，成块紧跟该页文字。铺满整页的图是扫描底图，文字层已经是它的识别结果，不再重复识别。
"""
import io
from typing import Dict, Tuple

from eva.contracts.document import Block, ParseLimits
from eva.contracts.errors import Code, EvaError
from eva.parsing import ocr
from eva.parsing.builder import Builder

_BACKGROUND_SHARE = 0.9  # 图片面积占页面的比例达到它，就当作扫描底图


def _render(page, dpi: int, box=None):
    area = page if box is None else page.crop(box)
    return area.to_image(resolution=dpi).original.convert("RGB")


def _embedded(page, number: int, limits: ParseLimits, builder: Builder, reader: ocr.Reader) -> None:
    left, top, right, bottom = page.bbox
    page_area = (right - left) * (bottom - top)
    ordered = sorted(page.images, key=lambda im: (round(im["top"]), im["x0"]))
    for image_no, im in enumerate(ordered, start=1):
        box = (max(im["x0"], left), max(im["top"], top), min(im["x1"], right), min(im["bottom"], bottom))
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        if (box[2] - box[0]) * (box[3] - box[1]) >= _BACKGROUND_SHARE * page_area:
            continue
        reader.read_into(builder, lambda: _render(page, limits.ocr_dpi, box), {"element": "image", "page": number, "image_no": image_no},
                         f"第{number}页的图{image_no}", embedded=True, size=tuple(int(v) for v in im["srcsize"]))


def parse(data: bytes, limits: ParseLimits) -> Tuple[Tuple[Block, ...], Tuple[dict, ...], Dict[int, bytes]]:
    import pdfplumber
    from pdfminer.pdfdocument import PDFPasswordIncorrect

    builder, reader = Builder(limits), ocr.Reader(limits)
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                text = (page.extract_text() or "").strip()
                anchor = {"element": "page", "page": number}
                if text:
                    builder.page(text, anchor)
                    _embedded(page, number, limits, builder, reader)
                else:
                    if not reader.read_into(builder, lambda: _render(page, limits.ocr_dpi), anchor, f"第{number}页"):
                        builder.unread(f"（第{number}页没有文字层，没有读到内容：OCR 已关闭、超出 OCR 张数上限，"
                                       "或 OCR 没有识别出文字，可能是空白页或纯图页）", anchor)
                page.flush_cache()
    except PDFPasswordIncorrect:
        raise EvaError(Code.FILE_UNSUPPORTED, "PDF 有密码，无法读取", reason="encrypted") from None
    return builder.finish(), reader.warnings(), reader.images
