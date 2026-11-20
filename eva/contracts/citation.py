"""回答里的引用格式。模型按它写，完成门禁按它核对，工具描述和提示词引用同一份说明。

文件引用：【文件名#b12「摘抄原文」】。文档类文件（docx、xlsx、pptx、pdf、md、图片）用块号 b12 或 b12-14，
纯文本（txt、csv）用行号 L12 或 L12-15。
网页引用：【w3「摘抄原文」】，编号是本会话里 web_search 返回的 [w3]。
企业数据引用：【q5「摘抄原文」】，编号是本会话里 query_company 返回的 [q5]。
摘抄必须是所引块、行、网页结果或企业数据记录里的原文，不是转述。
"""
import re
from dataclasses import dataclass
from typing import List, Tuple

FORMAT_HELP = "【文件名#b块号「摘抄原文」】，块号取自 read_file 返回的 [b块号]；纯文本文件用行号，写成【文件名#L起始行-结束行「摘抄原文」】"
WEB_FORMAT_HELP = "【w编号「摘抄原文」】，编号取自 web_search 返回的 [w编号]，摘抄取自该条的标题或正文"

QCC_FORMAT_HELP = "【q编号「摘抄原文」】，编号取自 query_company 返回的 [q编号]，摘抄取自该条记录"

KIND_WEB = "w"
KIND_QCC = "q"

_CITATION = re.compile(r"【([^【】#]+)#([bL])(\d+)(?:-[bL]?(\d+))?「([^」【】]*)」】")  # 范围的后半截带不带 b、L 都认
_WEB = re.compile(r"【w(\d+)「([^」【】]*)」】")
_MARKER = re.compile(r"【[^【】]*#[^【】]*】")  # 长得像引用的标记，格式不对的也要揪出来
_WEB_MARKER = re.compile(r"【w\d+(?:[-~、，,qw\d]*)(?:「[^【】]*)?】")
_QCC = re.compile(r"【q(\d+)「([^」【】]*)」】")
_QCC_MARKER = re.compile(r"【q\d+(?:[-~、，,qw\d]*)(?:「[^【】]*)?】")


@dataclass(frozen=True)
class Citation:
    file: str  # 网页和企业数据引用没有文件名，为空
    kind: str  # "b" 块号，"L" 行号，"w" 网页结果编号，"q" 企业数据记录编号
    first: int
    last: int
    quote: str
    raw: str


def find(text: str) -> Tuple[List[Citation], List[str]]:
    """返回 (格式正确的引用, 格式不对的类似引用的原文)，各自按在文中出现的先后排列。"""
    good = [(m.start(), Citation(m.group(1).strip(), m.group(2), int(m.group(3)), int(m.group(4) or m.group(3)), m.group(5), m.group(0)))
            for m in _CITATION.finditer(text)]
    good += [(m.start(), Citation("", KIND_WEB, int(m.group(1)), int(m.group(1)), m.group(2), m.group(0))) for m in _WEB.finditer(text)]
    good += [(m.start(), Citation("", KIND_QCC, int(m.group(1)), int(m.group(1)), m.group(2), m.group(0))) for m in _QCC.finditer(text)]
    bad = [(m.start(), m.group(0)) for pattern, ok in ((_MARKER, _CITATION), (_WEB_MARKER, _WEB), (_QCC_MARKER, _QCC))
           for m in pattern.finditer(text) if not ok.fullmatch(m.group(0))]
    return [c for _, c in sorted(good, key=lambda x: x[0])], [raw for _, raw in sorted(bad, key=lambda x: x[0])]

_SOURCE_NAMES = {KIND_WEB: "网页", KIND_QCC: "企业数据"}
_ANY = re.compile("|".join(p.pattern for p in (_CITATION, _WEB, _QCC)))


def readable(text: str) -> str:
    """给用户看的回答：引用标记换成"（来源：…）"，紧挨着的同一来源只留一个。摘抄和编号只对核对有用，对用户是乱码。"""
    def label(m):
        c = find(m.group(0))[0][0]
        return f"（来源：{c.file or _SOURCE_NAMES[c.kind]}）"
    out = _ANY.sub(label, text)
    return re.sub(r"(（来源：[^（）]+）)(?:\s*\1)+", r"\1", out)
