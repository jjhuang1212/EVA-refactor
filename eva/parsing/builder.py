"""把各格式读出的段落、表格、整页文字装成有序的块。块的大小、表格每块行数都来自 ParseLimits。"""
from typing import Dict, List, Optional, Sequence, Tuple

from eva.contracts.document import Block, ParseLimits
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import BlockKind


def split_text(text: str, limit: int) -> List[str]:
    """按行装满不超过 limit 字符的片段；单行超长就硬切，不丢内容。"""
    if len(text) <= limit:
        return [text]
    pieces, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:limit])
            line = line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            pieces.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        pieces.append(current)
    return pieces


def cell_text(value) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


class Builder:
    """流式段落（docx 的段落、md 的行块）攒到字符上限再成块；标题、表格、整页文字直接成块。"""

    def __init__(self, limits: ParseLimits, flow_element: str = "paragraph", flow_unit: str = "paragraphs"):
        self._limits, self._element, self._unit = limits, flow_element, flow_unit
        self.blocks: List[Block] = []
        self._path: List[Tuple[int, str]] = []  # (标题级别, 标题文字)
        self._buffer: List[str] = []
        self._first = self._last = 0

    @property
    def section_path(self) -> Tuple[str, ...]:
        return tuple(t for _, t in self._path)

    def _emit(self, kind: BlockKind, text: str, anchor: Dict) -> None:
        for piece in split_text(text, self._limits.block_max_chars):
            if piece.strip():
                if len(self.blocks) >= self._limits.max_blocks:
                    raise EvaError(Code.FILE_UNSUPPORTED, "文件的块数超过上限，不适合整份读取", reason="too_many_blocks",
                                   max_blocks=self._limits.max_blocks)
                self.blocks.append(Block(len(self.blocks), kind, piece, dict(anchor), self.section_path))

    def set_section(self, path: Sequence[str]) -> None:
        """直接指定章节链（sheet 名、幻灯片标题这类不分级的位置）。"""
        self.flush()
        self._path = [(0, t) for t in path]

    def flush(self) -> None:
        if self._buffer:
            self._emit(BlockKind.PARAGRAPH, "\n".join(self._buffer),
                       {"element": self._element, self._unit: [self._first, self._last]})
            self._buffer = []

    def paragraph(self, text: str, pos: int, end: Optional[int] = None) -> None:
        """pos 是这段在文件里的位置（段落序号或行号），end 是占多行时的最后一行，用于块的出处。"""
        if not text.strip():
            return
        if self._buffer and sum(len(t) + 1 for t in self._buffer) + len(text) > self._limits.block_max_chars:
            self.flush()
        if not self._buffer:
            self._first = pos
        self._buffer.append(text)
        self._last = pos if end is None else end

    def heading(self, text: str, level: int, pos: int) -> None:
        text = text.strip()
        if not text:
            return
        self.flush()
        while self._path and self._path[-1][0] >= level:
            self._path.pop()
        self._path.append((level, text))
        self._emit(BlockKind.HEADING, text, {"element": "heading", self._unit: [pos, pos]})

    def page(self, text: str, anchor: Dict) -> None:
        """整页、整张幻灯片这类自带位置的文字。"""
        self.flush()
        self._emit(BlockKind.PARAGRAPH, text, anchor)

    def ocr(self, result: Tuple[str, int], anchor: Dict) -> None:
        """result 是 ocr.Reader.read 的返回值 (文字, 置信度)。"""
        self.flush()
        self._emit(BlockKind.OCR, result[0], {**anchor, "ocr": True, "confidence": result[1]})

    def vision(self, anchor: Dict) -> int:
        """给要交视觉模型看的图留一个占位块，返回它的块序号；宿主拿到识别结果后替换文字，块序号不变。"""
        self.flush()
        ord_ = len(self.blocks)
        self._emit(BlockKind.VISION, "（等待看图）", {**anchor, "vision": True})
        return ord_

    def unread(self, note: str, anchor: Dict) -> None:
        self.flush()
        self._emit(BlockKind.UNREAD, note, anchor)

    def table(self, header: Optional[Sequence], rows: Sequence[Tuple[int, Sequence]], anchor: Dict) -> None:
        """rows 是 (行号, 单元格列表)。每块带表头（header 为 None 则没有）；行数和字符数先到哪个就在哪里分块。
        anchor 里补上本块的行区间。"""
        self.flush()
        head = ["行"] + [cell_text(c) for c in header or ()]
        head_lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] if header is not None else []
        chunk: List[str] = []
        first = last = 0
        size = sum(len(x) + 1 for x in head_lines)

        def close() -> None:
            nonlocal chunk, size
            if chunk:
                self._emit(BlockKind.TABLE, "\n".join(head_lines + chunk), {**anchor, "rows": [first, last]})
            chunk, size = [], sum(len(x) + 1 for x in head_lines)

        for number, cells in rows:
            line = "| " + " | ".join([str(number)] + [cell_text(c) for c in cells]) + " |"
            if chunk and (len(chunk) >= self._limits.table_rows_per_block
                          or size + len(line) + 1 > self._limits.block_max_chars):
                close()
            if not chunk:
                first = number
            chunk.append(line)
            last = number
            size += len(line) + 1
        close()

    def finish(self) -> Tuple[Block, ...]:
        self.flush()
        return tuple(self.blocks)

