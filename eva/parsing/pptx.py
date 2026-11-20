"""pptx：每张幻灯片的标题当章节，正文、表格、图片（OCR）、备注分别成块，出处是幻灯片号。"""
import io
from typing import Dict, Iterator, Tuple

from eva.contracts.document import Block, ParseLimits
from eva.parsing import ocr
from eva.parsing.builder import Builder


def _shapes(shapes) -> Iterator:
    for shape in shapes:
        if shape.shape_type == 6:  # MSO_SHAPE_TYPE.GROUP
            yield from _shapes(shape.shapes)
        else:
            yield shape


def parse(data: bytes, limits: ParseLimits) -> Tuple[Tuple[Block, ...], Tuple[dict, ...], Dict[int, bytes]]:
    from pptx import Presentation
    from pptx.shapes.picture import Picture
    from pptx.shapes.placeholder import PlaceholderPicture

    deck = Presentation(io.BytesIO(data))
    builder, reader = Builder(limits), ocr.Reader(limits)
    for number, slide in enumerate(deck.slides, start=1):
        title_shape = slide.shapes.title
        title = title_shape.text_frame.text.strip() if title_shape is not None and title_shape.has_text_frame else ""
        builder.set_section((title,) if title else ())
        if title:
            builder.page(title, {"element": "title", "slide": number})
        table_no, image_no, body = 0, 0, []
        for shape in _shapes(slide.shapes):
            if title_shape is not None and shape.shape_id == title_shape.shape_id:
                continue
            if shape.has_text_frame and shape.text_frame.text.strip():
                body.append(shape.text_frame.text.strip())
            elif getattr(shape, "has_table", False) and shape.has_table:
                if body:  # 表格前的正文先成块，保持页面上的先后顺序
                    builder.page("\n".join(body), {"element": "body", "slide": number})
                    body = []
                table_no += 1
                rows = [(i, [c.text for c in r.cells]) for i, r in enumerate(shape.table.rows, start=1)]
                anchor = {"element": "table", "slide": number, "table_no": table_no}
                if len(rows) > 1:
                    builder.table(rows[0][1], rows[1:], anchor)
                else:
                    builder.table(None, rows, anchor)
            elif isinstance(shape, (Picture, PlaceholderPicture)):
                if body:
                    builder.page("\n".join(body), {"element": "body", "slide": number})
                    body = []
                image_no += 1
                where = f"幻灯片{number}的图{image_no}"
                try:
                    blob = shape.image.blob
                except ValueError:  # 链接到外部文件的图片，文件里没有内容
                    blob = None
                if blob:
                    reader.read_into(builder, lambda: ocr.load(blob), {"element": "image", "slide": number, "image_no": image_no},
                                     where, embedded=True)
                else:
                    reader.unreadable(where)
        if body:
            builder.page("\n".join(body), {"element": "body", "slide": number})
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            builder.page(slide.notes_slide.notes_text_frame.text.strip(), {"element": "notes", "slide": number})
    return builder.finish(), reader.warnings(), reader.images
