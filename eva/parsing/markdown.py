"""md：# 标题建章节链，空行分段，围栏代码块里的 # 不当标题；出处是行号区间。"""
import re
from typing import Dict, Tuple

from eva.contracts.document import Block, ParseLimits
from eva.contracts.errors import Code, EvaError
from eva.parsing.builder import Builder

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def parse(data: bytes, limits: ParseLimits) -> Tuple[Tuple[Block, ...], Tuple[dict, ...], Dict[int, bytes]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise EvaError(Code.FILE_UNSUPPORTED, "文件不是 UTF-8 文本", reason="encoding") from None
    builder, fenced = Builder(limits, "text", "lines"), False
    pending, start = [], 0

    def end_paragraph() -> None:
        nonlocal pending
        if pending:
            builder.paragraph("\n".join(pending), start, start + len(pending) - 1)
            pending = []

    for number, line in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), start=1):
        if _FENCE.match(line):
            fenced = not fenced
        heading = None if fenced else _HEADING.match(line)
        if heading:
            end_paragraph()
            builder.heading(heading.group(2), len(heading.group(1)), number)
        elif not line.strip() and not fenced:
            end_paragraph()
        else:
            if not pending:
                start = number
            pending.append(line)
    end_paragraph()
    return builder.finish(), (), {}
