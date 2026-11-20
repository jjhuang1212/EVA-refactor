"""行内标记：引用、链接、粗体。引用的格式与 eva/contracts/citation.py 保持一致（tests 里逐条比对）。

  【文件名#b12「摘抄」】 / 【文件名#L12-15「摘抄」】   文件引用
  【w3「摘抄」】                                       网页引用，编号取自 web_search
  【q5「摘抄」】                                       企业数据引用，编号取自 query_company
  [文字](https://…)                                    超链接
  **粗体**
  [[公司|某某公司]]                                    关键信息标色，类型限 MARKS，颜色在 theme.yaml 的 marks
"""
import re

from eva_report.errors import ReportError

FILE_CITATION = r"【([^【】#]+)#([bL])(\d+)(?:-[bL]?(\d+))?「([^」【】]*)」】"
WEB_CITATION = r"【w(\d+)「([^」【】]*)」】"
QCC_CITATION = r"【q(\d+)「([^」【】]*)」】"
FILE_MARKER = r"【[^【】]*#[^【】]*】"  # 长得像文件引用的标记，格式不对的也要揪出来
WEB_MARKER = r"【w\d+(?:[-~、，,qw\d]*)(?:「[^【】]*)?】"  # 【q156-q184】【q3、q5】这类编号区间也算引用写错了，不能原样印进报告
QCC_MARKER = r"【q\d+(?:[-~、，,qw\d]*)(?:「[^【】]*)?】"
LINK = r"\[([^\[\]]+)\]\((https?://[^\s()]+)\)"
BOLD = r"\*\*([^*\n]+?)\*\*"
MARKS = ("公司", "地址", "人名", "时间", "数字")
MARK = r"\[\[([^\[\]|\n]+)\|([^\[\]|\n]+)\]\]"
MARK_MARKER = r"\[\[[^\[\]]*\]\]"

_TOKEN = re.compile("|".join(f"(?P<{n}>{p})" for n, p in (("file", FILE_CITATION), ("web", WEB_CITATION), ("qcc", QCC_CITATION), ("mark", MARK), ("link", LINK), ("bold", BOLD))))
_FILE, _WEB, _QCC, _MARK = re.compile(FILE_CITATION), re.compile(WEB_CITATION), re.compile(QCC_CITATION), re.compile(MARK)


def _check_markers(text):
    bad = [m.group(0) for pattern, ok in ((FILE_MARKER, _FILE), (WEB_MARKER, _WEB), (QCC_MARKER, _QCC))
           for m in re.finditer(pattern, text) if not ok.fullmatch(m.group(0))]
    if bad:
        raise ReportError(f"有引用格式不对：{'、'.join(bad[:3])}。文件引用写成【文件名#b块号「摘抄」】（纯文本文件用 L行号），网页引用写成【w编号「摘抄」】，企业数据引用写成【q编号「摘抄」】")


def _check_marks(text):
    for m in re.finditer(MARK_MARKER, text):
        g = _MARK.fullmatch(m.group(0))
        if g is None or g.group(1) not in MARKS:
            raise ReportError(f"标色格式不对：{m.group(0)}。写成 [[类型|文字]]，类型只能是 {'、'.join(MARKS)}")


def parse(text):
    """把一段文字拆成片段：{"t": "text"|"bold"|"mark"|"link"|"cite", ...}。引用的编号不在这里分配，见 tree.manifest。"""
    if not isinstance(text, str):
        raise ReportError(f"文字内容必须是字符串，收到 {type(text).__name__}")
    _check_markers(text)
    _check_marks(text)
    out, last = [], 0
    for m in _TOKEN.finditer(text):
        if m.start() > last:
            out.append({"t": "text", "text": text[last:m.start()]})
        if m.group("file"):
            g = _FILE.fullmatch(m.group("file")).groups()
            out.append({"t": "cite", "kind": g[1], "file": g[0].strip(), "first": int(g[2]), "last": int(g[3] or g[2]), "quote": g[4]})
        elif m.group("web"):
            g = _WEB.fullmatch(m.group("web")).groups()
            out.append({"t": "cite", "kind": "w", "file": "", "first": int(g[0]), "last": int(g[0]), "quote": g[1]})
        elif m.group("qcc"):
            g = _QCC.fullmatch(m.group("qcc")).groups()
            out.append({"t": "cite", "kind": "q", "file": "", "first": int(g[0]), "last": int(g[0]), "quote": g[1]})
        elif m.group("mark"):
            g = _MARK.fullmatch(m.group("mark")).groups()
            out.append({"t": "mark", "kind": g[0], "text": g[1]})
        elif m.group("link"):
            g = re.fullmatch(LINK, m.group("link")).groups()
            out.append({"t": "link", "text": g[0], "url": g[1]})
        else:
            out.append({"t": "bold", "text": re.fullmatch(BOLD, m.group("bold")).group(1)})
        last = m.end()
    if last < len(text):
        out.append({"t": "text", "text": text[last:]})
    return out
