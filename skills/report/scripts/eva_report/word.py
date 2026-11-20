"""文档树 -> docx（python-docx）。读同一份 theme.yaml，引用角标和文末来源与 PDF 一样来自同一份清单。

引用清单作为自定义部件 customXml/eva-citations.json 放在文件包里，交付前由宿主取出核对。
角标和来源之间用书签互相跳转，网页来源带超链接。
"""
import json
import os
from contextlib import contextmanager

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_BREAK
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor

from eva_report import inline as markup
from eva_report.errors import ReportError
from eva_report.render import cite_numbers, source_line, source_title
from eva_report.theme import load_theme
from eva_report.tree import cite_key

MANIFEST_PART = "/customXml/eva-citations.json"
_MANIFEST_RELTYPE = "http://schemas.eva.local/relationships/citations"
_PAGE_MM = {"A4": (210, 297), "A3": (297, 420), "Letter": (216, 279)}


def _rgb(hex_color):
    return RGBColor.from_string(hex_color.lstrip("#").upper())


def _set_font(rpr_owner, name):
    """西文和中文字体都设上，否则中文会落回默认字体。"""
    rpr = rpr_owner._element.get_or_add_rPr() if hasattr(rpr_owner, "_element") else rpr_owner
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        fonts.set(qn(attr), name)


def _shade(element_pr, fill):
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill.lstrip("#"))
    element_pr.append(shd)


def _border(element_pr, edge, color, size):
    borders = element_pr.find(qn("w:tcBorders")) if element_pr.tag == qn("w:tcPr") else element_pr.find(qn("w:pBdr"))
    if borders is None:
        borders = OxmlElement("w:tcBorders" if element_pr.tag == qn("w:tcPr") else "w:pBdr")
        element_pr.append(borders)
    b = OxmlElement(f"w:{edge}")
    b.set(qn("w:val"), "single")
    b.set(qn("w:sz"), str(size))
    b.set(qn("w:color"), color.lstrip("#"))
    borders.append(b)


class _Builder:
    def __init__(self, report, theme):
        self.report, self.theme, self.numbers = report, theme, cite_numbers(report)
        self.seen, self.bookmark_id = {}, 0
        self.doc = Document()
        self.font = theme["fonts"]["docx"]
        page, margin = theme["page"], theme["page"]["margin_mm"]
        section = self.doc.sections[0]
        section.page_width, section.page_height = (Mm(v) for v in _PAGE_MM[page["size"]])
        section.top_margin, section.bottom_margin = Mm(margin["top"]), Mm(margin["bottom"])
        section.left_margin, section.right_margin = Mm(margin["left"]), Mm(margin["right"])
        self.text_width_mm = _PAGE_MM[page["size"]][0] - margin["left"] - margin["right"]
        normal = self.doc.styles["Normal"]
        normal.font.name, normal.font.size = self.font, Pt(theme["sizes_pt"]["body"])
        normal.font.color.rgb = _rgb(theme["colors"]["text"])
        _set_font(normal.element.get_or_add_rPr(), self.font)
        normal.paragraph_format.space_after = Pt(5)
        normal.paragraph_format.line_spacing = theme["line_height"]
        self.doc.styles["Footer"].font.size = Pt(theme["sizes_pt"]["footer"])
        self.footer()

    # ---- 行内 ----

    def run(self, paragraph, text, size=None, bold=False, color=None, superscript=False):
        r = paragraph.add_run(text)
        r.bold = bold or None
        r.font.superscript = superscript or None
        if size:
            r.font.size = Pt(size)
        if color:
            r.font.color.rgb = _rgb(color)
        _set_font(r, self.font)
        return r

    def hyperlink(self, paragraph, text, url=None, anchor=None, superscript=False):
        link = OxmlElement("w:hyperlink")
        link.set(qn("w:history"), "1")  # Word 自己写的内部链接都带它
        if url:
            link.set(qn("r:id"), paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True))
        else:
            link.set(qn("w:anchor"), anchor)
        r = OxmlElement("w:r")
        rpr = OxmlElement("w:rPr")
        color = OxmlElement("w:color")
        color.set(qn("w:val"), self.theme["colors"]["link"].lstrip("#"))
        rpr.append(color)
        if superscript:
            vert = OxmlElement("w:vertAlign")
            vert.set(qn("w:val"), "superscript")
            rpr.append(vert)
        _set_font(rpr, self.font)
        r.append(rpr)
        t = OxmlElement("w:t")
        t.text = text
        t.set(qn("xml:space"), "preserve")
        r.append(t)
        link.append(r)
        paragraph._p.append(link)

    @contextmanager
    def bookmark(self, paragraph, name):
        """书签包住 with 块里加进段落的内容，内部超链接靠它跳转。不用零宽书签：Word 里它不可靠。"""
        self.bookmark_id += 1
        start, end = OxmlElement("w:bookmarkStart"), OxmlElement("w:bookmarkEnd")
        start.set(qn("w:id"), str(self.bookmark_id))
        start.set(qn("w:name"), name)
        end.set(qn("w:id"), str(self.bookmark_id))
        paragraph._p.append(start)
        yield
        paragraph._p.append(end)

    def inline(self, paragraph, text, size=None, color=None):
        for seg in markup.parse(text):
            kind = seg["t"]
            if kind == "text":
                self.run(paragraph, seg["text"], size, color=color)
            elif kind == "bold":
                self.run(paragraph, seg["text"], size, bold=True, color=color)
            elif kind == "mark":
                r = self.run(paragraph, seg["text"], size, color=color)
                shd = OxmlElement("w:shd")
                shd.set(qn("w:val"), "clear")
                shd.set(qn("w:fill"), self.theme["marks"][seg["kind"]].lstrip("#"))
                r._r.get_or_add_rPr().append(shd)
            elif kind == "link":
                self.hyperlink(paragraph, seg["text"], url=seg["url"])
            else:
                no = self.numbers[cite_key(seg)]
                self.seen[no] = self.seen.get(no, 0) + 1
                with self.bookmark(paragraph, f"cite_{no}_{self.seen[no]}"):
                    self.hyperlink(paragraph, f"[{no}]", anchor=f"src_{no}", superscript=True)

    def paragraph(self, text, **kw):
        p = self.doc.add_paragraph()
        self.inline(p, text, **kw)
        return p

    # ---- 节点 ----

    def footer(self):
        p = self.doc.sections[0].footer.paragraphs[0]
        small, faint = self.theme["sizes_pt"]["footer"], self.theme["colors"]["faint"]
        self.run(p, f"{self.report.title}    ", small, color=faint)
        for code in ("PAGE", " / ", "NUMPAGES"):
            if code == " / ":
                self.run(p, code, small, color=faint)
                continue
            r = self.run(p, "", small, color=faint)
            for kind, text in (("begin", None), (None, code), ("end", None)):
                el = OxmlElement("w:fldChar" if kind else "w:instrText")
                if kind:
                    el.set(qn("w:fldCharType"), kind)
                else:
                    el.text = f" {text} "
                    el.set(qn("xml:space"), "preserve")
                r._r.append(el)

    def node(self, n, counter):
        sizes, colors = self.theme["sizes_pt"], self.theme["colors"]
        kind = n["type"]
        if kind == "cover":
            if n.get("eyebrow"):
                self.run(self.doc.add_paragraph(), n["eyebrow"], sizes["small"], color=colors["muted"])
            p = self.doc.add_paragraph()
            self.run(p, self.report.title, sizes["title"], bold=True, color=colors["brand"])
            if n["subtitle"]:
                self.run(self.doc.add_paragraph(), n["subtitle"], sizes["subtitle"], color=colors["muted"])
            if n["meta"]:
                p = self.doc.add_paragraph()
                for k, v in n["meta"]:
                    self.run(p, f"{k}  ", sizes["small"], color=colors["muted"])
                    self.run(p, f"{v}        ", sizes["small"])
        elif kind == "heading":
            level = n["level"]
            if level == 1:
                counter[0] += 1
            style = self.doc.add_paragraph(style=f"Heading {level}")
            label = f"{counter[0]:02d}  {n['text']}" if level == 1 else n["text"]
            self.run(style, label, sizes[f"h{level}"], bold=True, color=colors["brand"] if level < 3 else colors["text"])
            style.paragraph_format.space_before, style.paragraph_format.keep_with_next = Pt(14 if level == 1 else 9), True
            if level == 1:
                _border(style._p.get_or_add_pPr(), "bottom", colors["brand"], 8)
        elif kind == "paragraph":
            self.paragraph(n["text"])
        elif kind == "bullets":
            for item in n["items"]:
                self.paragraph_in_style(item, "List Number" if n["ordered"] else "List Bullet")
        elif kind == "table":
            self.table(n)
        elif kind == "figure":
            self.figure(n)
        elif kind == "callout":
            tone = self.theme["tones"][n["tone"]]
            p = self.doc.add_paragraph()
            self.inline(p, n["text"], color=tone["fg"])
            ppr = p._p.get_or_add_pPr()
            _border(ppr, "left", tone["fg"], 24)
            _shade(ppr, tone["bg"])
            p.paragraph_format.left_indent = Mm(1)
        else:  # page_break
            self.doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    def paragraph_in_style(self, text, style):
        p = self.doc.add_paragraph(style=style)
        self.inline(p, text)

    def table(self, n):
        colors, sizes = self.theme["colors"], self.theme["sizes_pt"]
        if n["caption"]:
            cap = self.paragraph(n["caption"], size=sizes["small"], color=colors["muted"])
            cap.paragraph_format.keep_with_next = True
        table = self.doc.add_table(rows=1, cols=len(n["headers"]))
        table.alignment, table.autofit = WD_TABLE_ALIGNMENT.CENTER, False
        weights = n["col_widths"] or [1] * len(n["headers"])
        widths = [Mm(self.text_width_mm * w / sum(weights)) for w in weights]
        for column, width in zip(table.columns, widths):
            column.width = width  # 表格网格里的列宽，LibreOffice 和 Word 都按它排
        for i, header in enumerate(n["headers"]):
            cell = table.rows[0].cells[i]
            cell.width = widths[i]
            _shade(cell._tc.get_or_add_tcPr(), colors["brand"])
            self.run(cell.paragraphs[0], header, sizes["small"], color="#ffffff")
        for r, row in enumerate(n["rows"]):
            cells = table.add_row().cells
            for i, text in enumerate(row):
                cells[i].width = widths[i]
                tcpr = cells[i]._tc.get_or_add_tcPr()
                _border(tcpr, "bottom", colors["line"], 4)
                if r % 2 == 1:
                    _shade(tcpr, colors["zebra"])
                self.inline(cells[i].paragraphs[0], text, size=sizes["small"])
        self.doc.add_paragraph()

    def figure(self, n):
        if n["path"].lower().endswith(".svg"):
            raise ReportError("docx 不支持 svg 图片，请改存成 png")
        p = self.doc.add_paragraph()
        p.alignment = 1
        p.add_run().add_picture(n["path"], width=Mm(self.text_width_mm * n["width_pct"] / 100))
        if n["caption"]:
            cap = self.paragraph(n["caption"], size=self.theme["sizes_pt"]["small"], color=self.theme["colors"]["muted"])
            cap.alignment = 1

    def sources(self):
        entries = self.report.manifest()
        if not entries:
            return
        sizes, colors = self.theme["sizes_pt"], self.theme["colors"]
        head = self.doc.add_paragraph(style="Heading 1")
        self.run(head, "来源", sizes["h1"], bold=True, color=colors["brand"])
        _border(head._p.get_or_add_pPr(), "bottom", colors["brand"], 8)
        for e in entries:
            p = self.doc.add_paragraph()
            with self.bookmark(p, f"src_{e['no']}"):
                self.run(p, f"[{e['no']}] ", sizes["small"], bold=True, color=colors["brand"])
            if e["kind"] == "w":
                self.hyperlink(p, source_title(e), url=e["url"])
                self.run(p, f"  {e['url']}", sizes["small"], color=colors["muted"])
            else:
                self.run(p, source_line(e), sizes["small"])
            self.run(p, f"  「{e['quote']}」  ", sizes["small"], color=colors["muted"])
            self.hyperlink(p, "回到正文", anchor=f"cite_{e['no']}_1")

    def embed_manifest(self):
        blob = json.dumps(self.report.embedded(), ensure_ascii=False).encode("utf-8")
        part = Part(PackURI(MANIFEST_PART), "application/json", blob, self.doc.part.package)
        self.doc.part.relate_to(part, _MANIFEST_RELTYPE)


def save(report, path, theme_path=None):
    """渲染成 docx 写到 path，返回 {"bytes", "citations"}。"""
    report.validate()
    builder, counter = _Builder(report, load_theme(theme_path)), [0]
    for n in report.nodes:
        builder.node(n, counter)
    builder.sources()
    builder.embed_manifest()
    builder.doc.core_properties.title = report.title
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    builder.doc.save(str(path))
    return {"bytes": os.path.getsize(path), "citations": len(report.manifest())}
