"""docx：按文档顺序读段落、表格、嵌套表格、文本框、脚注和尾注。标题链进 section_path，表格带表号；没有页码（docx 的页是排版结果，不可靠）。

脚注、尾注、文本框、嵌入的图片（OCR）紧跟在引用它们的段落或表格之后成块，章节链沿用该段所在章节。页眉页脚不读。
"""
import io
import re
from typing import Dict, List, Tuple

from eva.contracts.document import Block, ParseLimits
from eva.parsing import ocr
from eva.parsing.builder import Builder

_HEADING = re.compile(r"^(?:heading|标题)\s*(\d)$", re.I)
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_V = "{urn:schemas-microsoft-com:vml}"
_IMAGE_REFS = ((f"{_A}blip", f"{_R}embed"), (f"{_V}imagedata", f"{_R}id"))  # (图片元素, 指向图片部件的属性)
_NOTES = (("footnote", "/footnotes", "脚注"), ("endnote", "/endnotes", "尾注"))  # (元素名, 关系类型后缀, 显示名)
_SEPARATORS = ("separator", "continuationSeparator", "continuationNotice")


def _level(paragraph) -> int:
    """标题级别，0 = 不是标题。先看样式名，再看段落自己的大纲级别。"""
    style = paragraph.style.name if paragraph.style is not None else ""
    m = _HEADING.match(style.strip())
    if m:
        return int(m.group(1))
    if style.strip().lower() in ("title", "标题"):
        return 1
    outline = paragraph._p.find(f"{_W}pPr/{_W}outlineLvl")
    if outline is not None and outline.get(f"{_W}val", "").isdigit() and int(outline.get(f"{_W}val")) < 9:
        return int(outline.get(f"{_W}val")) + 1
    return 0


def _is_list(paragraph) -> bool:
    """编号可以写在段落上，也可以由样式带（如 List Bullet）。"""
    style = paragraph.style
    return paragraph._p.find(f"{_W}pPr/{_W}numPr") is not None or (
        style is not None and style.element.find(f"{_W}pPr/{_W}numPr") is not None)


def _text_of(element) -> str:
    return "\n".join("".join(t.text or "" for t in p.iter(f"{_W}t")) for p in element.iter(f"{_W}p")).strip()


def _notes(doc) -> Dict[str, Dict[str, str]]:
    """{元素名: {id: 正文}}。注释部件在 docx 里是独立的 xml，python-docx 不直接暴露。"""
    from docx.oxml import parse_xml
    found = {}
    for name, suffix, _ in _NOTES:
        found[name] = {}
        for rel in doc.part.rels.values():
            if rel.reltype.endswith(suffix):
                for note in parse_xml(rel.target_part.blob).findall(f"{_W}{name}"):
                    if note.get(f"{_W}type") not in _SEPARATORS:
                        found[name][note.get(f"{_W}id")] = _text_of(note)
    return found


def _own(element, tag):
    """element 下属于它自己的 tag 后代，不进入嵌套的 w:tbl（嵌套表单独处理，不然脚注会被数两次）。"""
    for child in element.iterchildren():
        if child.tag != f"{_W}tbl":
            if child.tag == tag:
                yield child
            yield from _own(child, tag)


def _textboxes(element) -> List[str]:
    """段落里的文本框正文。mc:Fallback 里是同一内容的旧式备份，跳过以免重复。"""
    out = []
    for box in _own(element, f"{_W}txbxContent"):
        if not any(a.tag == f"{_MC}Fallback" for a in box.iterancestors()):
            text = _text_of(box)
            if text:
                out.append(text)
    return out


class _Reader:
    def __init__(self, doc, limits: ParseLimits):
        self.doc, self.builder, self.notes = doc, Builder(limits), _notes(doc)
        self.table_no, self.counts = 0, {"footnote": 0, "endnote": 0}
        self.ocr, self.image_no = ocr.Reader(limits), 0

    def table(self, table, parent: int = 0) -> None:
        self.table_no += 1
        number, rows, nested = self.table_no, [], []
        for row_no, row in enumerate(table.rows, start=1):
            seen, cells = set(), []
            for cell in row.cells:  # 合并单元格会重复返回同一个格，只取一次
                if id(cell._tc) in seen:
                    continue
                seen.add(id(cell._tc))
                nested += cell.tables
                cells.append(cell.text + ("（含嵌套表）" if cell.tables else ""))
            rows.append((row_no, cells))
        head, body = (rows[0][1], rows[1:]) if len(rows) > 1 else (None, rows)
        anchor = {"element": "table", "table_no": number}
        self.builder.table(head, body, {**anchor, "parent_table": parent} if parent else anchor)
        self.attachments(table._tbl, {"table_no": number})
        for inner in nested:  # 嵌套表紧跟在外层表之后，编号接着往后排
            self.table(inner, number)

    @staticmethod
    def _image_ids(element):
        """element 里嵌入图片的关系 id，按出现顺序；外链图片没有 id，给 None。mc:Fallback 里是同一张图的旧式备份，跳过。"""
        for tag, attr in _IMAGE_REFS:
            for node in _own(element, tag):
                if not any(a.tag == f"{_MC}Fallback" for a in node.iterancestors()):
                    yield node.get(attr)

    def attachments(self, element, where: Dict) -> None:
        """紧跟在引用它们的段落或表格后面：文本框、嵌入图片、脚注、尾注。"""
        for text in _textboxes(element):
            self.builder.page(text, {"element": "textbox", **where})
        for rid in self._image_ids(element):
            self.image_no += 1
            shown = f"表{where['table_no']}" if "table_no" in where else f"段落{where['paragraphs'][0]}"
            where_text = f"{shown}的图{self.image_no}"
            try:
                blob = self.doc.part.related_parts[rid].blob if rid else None
            except KeyError:
                blob = None
            if blob:
                self.ocr.read_into(self.builder, lambda: ocr.load(blob), {"element": "image", "image_no": self.image_no, **where},
                                   where_text, embedded=True)
            else:
                self.ocr.unreadable(where_text)
        for name, _, shown in _NOTES:
            for ref in _own(element, f"{_W}{name}Reference"):
                self.counts[name] += 1
                text = self.notes[name].get(ref.get(f"{_W}id"), "")
                if text:
                    self.builder.page(f"[{shown}{self.counts[name]}] {text}",
                                      {"element": name, "note_no": self.counts[name]})

def parse(data: bytes, limits: ParseLimits) -> Tuple[Tuple[Block, ...], Tuple[dict, ...], Dict[int, bytes]]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(data))
    reader, position = _Reader(doc, limits), 0
    for child in doc.element.body.iterchildren():
        if child.tag == f"{_W}p":
            paragraph = Paragraph(child, doc)
            position += 1
            text, level = paragraph.text.strip(), _level(paragraph)
            if level:
                reader.builder.heading(text, level, position)
            else:
                reader.builder.paragraph(f"- {text}" if text and _is_list(paragraph) else text, position)
            reader.attachments(child, {"paragraphs": [position, position]})
        elif child.tag == f"{_W}tbl":
            reader.table(Table(child, doc))
    return reader.builder.finish(), reader.ocr.warnings(), reader.ocr.images
