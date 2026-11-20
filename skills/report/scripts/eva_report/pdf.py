"""文档树 -> PDF（WeasyPrint）。版式全在这里的样式模板和 theme.yaml，内容不在这里改。

三件事做得比较较真：
- 字体自检：缺中文字体时 WeasyPrint 照样"渲染成功"，PDF 里每个汉字都是方块。所以渲染前先排一段探针文字再抽回来比对，
  不过就报错，不出一份看着完整、其实读不了的报告。
- 不联网：图片以内嵌数据的方式放进去，渲染时除 data: 外的任何地址一律拒绝。
- 引用清单随 PDF 一起带走：作为附件 eva-citations.json 嵌在文件里，交付前由宿主取出核对。
"""
import base64
import html as _html
import io
import json
import mimetypes
import os
from string import Template

from eva_report.errors import ReportError
from eva_report.inline import MARKS
from eva_report.render import cite_numbers, source_line, source_title
from eva_report.tree import cite_key
from eva_report.theme import load_theme

ATTACHMENT_NAME = "eva-citations.json"
_PROBE = "股权关系核查0123456789ABCXYZabcxyz（经企查查）【】「」·"

_CSS = Template("""
@page {
  size: $page_size;
  margin: ${top}mm ${right}mm ${bottom}mm ${left}mm;
  font-family: $font;
  @bottom-left { content: string(running-title); font-size: ${footer}pt; color: $faint; padding-top: 3mm; }
  @bottom-right { content: counter(page) " / " counter(pages); font-size: ${footer}pt; color: $faint; padding-top: 3mm; }
}
body { font-family: $font; font-size: ${body}pt; line-height: $line_height; color: $text; margin: 0; }
.rt { string-set: running-title content(); height: 0; overflow: hidden; font-size: 0; }
a { color: $link; text-decoration: none; }
.cover { margin: 0 0 8mm; }
.eyebrow { font-size: ${footer}pt; letter-spacing: 0.34em; color: $muted; text-transform: uppercase; margin: 0 0 2.5mm; }
h1.title { font-size: ${title}pt; font-weight: 700; color: $brand; margin: 0 0 3mm; letter-spacing: 0.02em; }
.subtitle { font-size: ${subtitle}pt; color: $muted; margin: 0 0 4mm; }
.meta { display: table; width: 100%; border-top: 0.5pt solid $line; border-bottom: 0.5pt solid $line; padding: 2mm 0; }
.meta-item { display: table-cell; font-size: ${small}pt; padding-right: 5mm; }
.meta-key { color: $muted; margin-right: 1.5mm; }
h2.section { font-size: ${h1}pt; font-weight: 700; color: $brand; margin: 7mm 0 2.5mm; padding-bottom: 1.2mm;
  border-bottom: 1pt solid $brand; page-break-after: avoid; bookmark-level: 1; bookmark-label: content(); }
h2.section .no { font-size: ${small}pt; color: $faint; margin-right: 2mm; letter-spacing: 0.06em; }
h3.sub { font-size: ${h2}pt; font-weight: 700; color: $brand; margin: 5mm 0 2mm; page-break-after: avoid;
  bookmark-level: 2; bookmark-label: content(); }
h4.sub { font-size: ${h3}pt; font-weight: 700; color: $text; margin: 4mm 0 1.5mm; page-break-after: avoid;
  bookmark-level: 3; bookmark-label: content(); }
p { margin: 0 0 2.5mm; }
ul, ol { margin: 0 0 2.5mm; padding-left: 6mm; }
li { margin: 0 0 1mm; }
sup.cite { font-size: ${small}pt; line-height: 1; }
sup.cite a { color: $link; }
.table-label { font-size: ${small}pt; color: $muted; margin: 0 0 1.2mm; page-break-after: avoid; }
table { width: 100%; border-collapse: collapse; margin: 0 0 3mm; font-size: ${small}pt; table-layout: fixed; }
th { background: $brand; color: #ffffff; font-weight: 400; text-align: left; padding: 1.6mm 2mm; letter-spacing: 0.04em; }
td { padding: 1.6mm 2mm; border-bottom: 0.5pt solid $line; vertical-align: top; overflow-wrap: anywhere; }
tbody tr:nth-child(even) td { background: $zebra; }
thead { display: table-header-group; }
tr { page-break-inside: avoid; }
figure { margin: 0 0 3mm; text-align: center; page-break-inside: avoid; }
figure img { max-width: 100%; }
figcaption { font-size: ${small}pt; color: $muted; margin-top: 1mm; }
.callout { padding: 2mm 3mm; margin: 0 0 3mm; border-radius: 1mm; page-break-inside: avoid; }
$tones
.mk { border-radius: 0.6mm; padding: 0 0.4mm; }
$marks
.sources .src { font-size: ${small}pt; margin: 0 0 1.6mm; padding-left: 8mm; text-indent: -8mm; page-break-inside: avoid; }
.sources .n { color: $brand; font-weight: 700; margin-right: 1.5mm; }
.sources .url { color: $muted; word-break: break-all; }
.sources .quote { color: $muted; }
.sources .back { color: $faint; margin-left: 2mm; }
""")


def _esc(text):
    return _html.escape(text, quote=True)


def _css(theme):
    page, colors, sizes = theme["page"], theme["colors"], theme["sizes_pt"]
    tones = "\n".join(f".callout-{name} {{ color: {t['fg']}; background: {t['bg']}; border-left: 1.2mm solid {t['fg']}; }}"
                      for name, t in theme["tones"].items())
    marks = "\n".join(f".mk-{i} {{ background: {theme['marks'][name]}; }}" for i, name in enumerate(MARKS))
    font = ", ".join(f'"{f}"' if " " in f else f for f in theme["fonts"]["sans"])
    return _CSS.substitute(page_size=page["size"], font=font, line_height=theme["line_height"], tones=tones, marks=marks,
                           **page["margin_mm"], **colors, **sizes)


def _check_fonts(theme):
    """用探针文字排一页再抽回文字，缺字就报错。"""
    html_text = f'<html><head><meta charset="utf-8"><style>{_css(theme)}</style></head><body><p>{_esc(_PROBE)}</p></body></html>'
    extracted = _extract_text(_write(html_text, [])[0])
    missing = [ch for ch in _PROBE if ch not in extracted]
    if missing:
        raise ReportError(f"本机排不出这些字：{''.join(missing)}。主题里的中文字体一个都没装（theme.yaml 的 fonts.sans），"
                          "先装 Noto Sans CJK SC 之类的中文字体，不要在缺字体时出报告")


def _extract_text(pdf_bytes):
    try:
        from pypdf import PdfReader
        return PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text() or ""
    except ImportError:
        pass
    try:
        import pdfplumber
    except ImportError:
        raise ReportError("没有 pypdf 或 pdfplumber，无法做字体自检") from None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return pdf.pages[0].extract_text() or ""


def _write(html_text, attachments):
    try:
        from weasyprint import HTML, URLFetcher
    except OSError as e:  # 缺 Pango/GLib 等系统库时 import 阶段就抛 OSError，不转的话模型只看到一屏 traceback
        raise ReportError(f"本机缺少 WeasyPrint 依赖的系统库（Pango/GLib），出不了 PDF：{e}。"
                          "这是本机环境问题，重试和改代码都没用；改出 docx，并在回答里告诉用户 PDF 因本机缺少依赖没能生成") from e
    from weasyprint.urls import FatalURLFetchingError
    fetcher = URLFetcher(allowed_protocols={"data"}, fail_on_errors=True)  # 只认内嵌数据，外部地址一律拒绝
    try:
        document = HTML(string=html_text, url_fetcher=fetcher).render()
    except FatalURLFetchingError as e:  # 它继承 BaseException，不转成 ReportError 会绕过调用方的 except Exception
        raise ReportError(f"渲染时不允许读取外部地址：{e}") from e
    buffer = io.BytesIO()
    document.write_pdf(buffer, attachments=attachments)
    return buffer.getvalue(), len(document.pages)


class _Html:
    """把文档树逐节点转成 HTML。引用角标和文末来源共用同一份清单。"""

    def __init__(self, report):
        self.report, self.numbers = report, cite_numbers(report)
        self.seen = {}  # 清单编号 -> 正文里已出现几次，给每处角标一个 id，来源条目才能跳回去

    def inline(self, text):
        out = []
        from eva_report import inline as markup
        from eva_report.tree import cite_key
        for seg in markup.parse(text):
            kind = seg["t"]
            if kind == "text":
                out.append(_esc(seg["text"]).replace("\n", "<br>"))
            elif kind == "bold":
                out.append(f"<strong>{_esc(seg['text'])}</strong>")
            elif kind == "mark":
                out.append(f'<span class="mk mk-{MARKS.index(seg["kind"])}">{_esc(seg["text"])}</span>')
            elif kind == "link":
                out.append(f'<a href="{_esc(seg["url"])}">{_esc(seg["text"])}</a>')
            else:
                no = self.numbers[cite_key(seg)]
                self.seen[no] = self.seen.get(no, 0) + 1
                out.append(f'<sup class="cite"><a id="cite-{no}-{self.seen[no]}" href="#src-{no}">[{no}]</a></sup>')
        return "".join(out)

    def node(self, n, counter):
        kind = n["type"]
        if kind == "cover":
            meta = "".join(f'<span class="meta-item"><span class="meta-key">{_esc(k)}</span>{_esc(v)}</span>' for k, v in n["meta"])
            return ('<div class="cover">' + (f'<div class="eyebrow">{_esc(n["eyebrow"])}</div>' if n.get("eyebrow") else "")
                    + f'<h1 class="title">{_esc(self.report.title)}</h1>'
                    + (f'<p class="subtitle">{_esc(n["subtitle"])}</p>' if n["subtitle"] else "")
                    + (f'<div class="meta">{meta}</div>' if meta else "") + "</div>")
        if kind == "heading":
            if n["level"] == 1:
                counter[0] += 1
                return f'<h2 class="section"><span class="no">{counter[0]:02d}</span>{_esc(n["text"])}</h2>'
            tag = "h3" if n["level"] == 2 else "h4"
            return f'<{tag} class="sub">{_esc(n["text"])}</{tag}>'
        if kind == "paragraph":
            return f"<p>{self.inline(n['text'])}</p>"
        if kind == "bullets":
            tag = "ol" if n["ordered"] else "ul"
            return f"<{tag}>" + "".join(f"<li>{self.inline(i)}</li>" for i in n["items"]) + f"</{tag}>"
        if kind == "table":
            return self.table(n)
        if kind == "figure":
            return self.figure(n)
        if kind == "callout":
            return f'<div class="callout callout-{n["tone"]}">{self.inline(n["text"])}</div>'
        return '<div style="page-break-after: always"></div>'  # page_break

    def table(self, n):
        cols = ""
        if n["col_widths"]:
            total = sum(n["col_widths"])
            cols = "<colgroup>" + "".join(f'<col style="width: {w * 100 / total:.2f}%">' for w in n["col_widths"]) + "</colgroup>"
        head = "".join(f"<th>{_esc(h)}</th>" for h in n["headers"])
        body = "".join("<tr>" + "".join(f"<td>{self.inline(c)}</td>" for c in row) + "</tr>" for row in n["rows"])
        label = f'<p class="table-label">{self.inline(n["caption"])}</p>' if n["caption"] else ""
        return f"{label}<table>{cols}<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    def figure(self, n):
        with open(n["path"], "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        mime = "image/svg+xml" if n["path"].lower().endswith(".svg") else mimetypes.guess_type(n["path"])[0]
        caption = f"<figcaption>{self.inline(n['caption'])}</figcaption>" if n["caption"] else ""
        return f'<figure><img style="width: {n["width_pct"]}%" src="data:{mime};base64,{data}">{caption}</figure>'

    def sources(self):
        entries = self.report.manifest()
        if not entries:
            return ""
        view = self.report.source_view or {"title": "来源", "notes": {}}
        rows = []
        for e in entries:
            if e["kind"] == "w":
                what = (f'<a href="{_esc(e["url"])}">{_esc(source_title(e))}</a> <span class="url">{_esc(e["url"])}</span>')
                tail = f' <span class="quote">「{_esc(e["quote"])}」</span>'
            elif cite_key(e) in view["notes"]:
                what, tail = f"{_esc(source_title(e))} · {_esc(view['notes'][cite_key(e)])}", ""
            else:
                what = _esc(source_line(e))
                tail = f' <span class="quote">「{_esc(e["quote"])}」</span>'
            rows.append(f'<div class="src" id="src-{e["no"]}"><span class="n">[{e["no"]}]</span>{what}{tail}'
                        f'<a class="back" href="#cite-{e["no"]}-1">回到正文</a></div>')
        return f'<div class="sources"><h2 class="section">{_esc(view["title"])}</h2>' + "".join(rows) + "</div>"


def save(report, path, theme_path=None):
    """渲染成 PDF 写到 path，返回 {"pages", "bytes", "citations"}。内容不合格、缺中文字体都直接报错，不出残缺的文件。"""
    report.validate()
    theme = load_theme(theme_path)
    _check_fonts(theme)
    builder, counter = _Html(report), [0]
    body = "".join(builder.node(n, counter) for n in report.nodes) + builder.sources()
    page = (f'<html><head><meta charset="utf-8"><title>{_esc(report.title)}</title><style>{_css(theme)}</style></head>'
            f'<body><div class="rt">{_esc(report.title)}</div>{body}</body></html>')
    from weasyprint import Attachment
    manifest = json.dumps(report.embedded(), ensure_ascii=False).encode("utf-8")
    data, pages = _write(page, [Attachment(string=manifest, name=ATTACHMENT_NAME, description="引用清单")])
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return {"pages": pages, "bytes": len(data), "citations": len(report.manifest())}
