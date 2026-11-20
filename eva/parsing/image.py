"""图片文件（png、jpg）：整张图 OCR 成一个文字块。手机拍的图按 EXIF 转正再识别。"""
from typing import Dict, Tuple

from eva.contracts.document import Block, ParseLimits
from eva.parsing import ocr
from eva.parsing.builder import Builder


def parse(data: bytes, limits: ParseLimits) -> Tuple[Tuple[Block, ...], Tuple[dict, ...], Dict[int, bytes]]:
    builder, reader, anchor = Builder(limits), ocr.Reader(limits), {"element": "image"}
    if not reader.read_into(builder, lambda: ocr.load(data), anchor, "图片"):
        builder.unread("（图片没有读取：OCR 已关闭，或图片里没有识别出文字，可能是照片、图表或空白图）", anchor)
    return builder.finish(), reader.warnings(), reader.images
