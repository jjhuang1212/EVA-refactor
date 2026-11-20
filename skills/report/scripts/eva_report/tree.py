"""文档树：与输出格式无关的报告内容。模型拼装它，渲染器（pdf、md、docx）读它。

树就是一份 JSON（节点是字典），所以能存盘再读回来：run_python 的进程和变量不保留，报告分几次脚本写时靠它接力。
引用清单由树生成（manifest），渲染出来的角标和文末来源都来自同一份清单，核对也核对这一份。
"""
import json
import os
import re

from eva_report import inline
from eva_report.errors import ReportError

TONES = ("info", "ok", "warn", "bad")
_USCC = re.compile(r"^[0-9A-Z]{18}$")
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".svg")
# 各节点里带行内标记的字段；值可以是字符串、字符串列表或字符串的二维列表
_INLINE = {"paragraph": ("text",), "bullets": ("items",), "table": ("rows", "caption"), "figure": ("caption",), "callout": ("text",)}


def _text(v, what, allow_empty=False):
    if not isinstance(v, str):
        raise ReportError(f"{what}必须是字符串，收到 {type(v).__name__}")
    if not v.strip() and not allow_empty:
        raise ReportError(f"{what}不能是空的")
    return v


def _flatten(value):
    if isinstance(value, list):
        for v in value:
            yield from _flatten(v)
    elif value is not None:
        yield value


class Report:
    def __init__(self, title):
        self.title = _text(title, "报告标题").strip()
        self.nodes = []
        self.web_sources = {}  # "w3" -> {"title", "url", "site", "date"}
        self.scenario_decl = None
        self.source_view = None  # PDF 文末来源一节的标题和每条的说明，见 source_notes；不存进报告文件

    # ---- 拼装 ----

    def _add(self, node):
        for field in _INLINE.get(node["type"], ()):
            for cell in _flatten(node.get(field)):
                inline.parse(cell)  # 格式不对当场报，不等到渲染
        self.nodes.append(node)

    def cover(self, subtitle="", meta=(), eyebrow=""):
        """封面：标题用报告标题；meta 是 [(名称, 内容)]，例如数据截止日期；eyebrow 是标题上方的小字眉题（英文大写，如 INTERVIEW TRANSCRIPT），
        PDF 里字母拉开间距显示。只能有一个，且必须是第一个节点。"""
        if self.nodes:
            raise ReportError("封面必须是第一个节点，且只能有一个")
        pairs = [[_text(k, "封面信息的名称"), _text(v, "封面信息的内容")] for k, v in meta]
        self._add({"type": "cover", "subtitle": _text(subtitle, "副标题", True), "meta": pairs, "eyebrow": _text(eyebrow, "眉题", True)})

    def heading(self, text, level=1):
        if level not in (1, 2, 3):
            raise ReportError("标题级别只能是 1、2、3")
        self._add({"type": "heading", "level": level, "text": _text(text, "标题")})

    def paragraph(self, text):
        self._add({"type": "paragraph", "text": _text(text, "段落")})

    def bullets(self, items, ordered=False):
        if not isinstance(items, (list, tuple)) or not items:
            raise ReportError("列表至少要有一项")
        self._add({"type": "bullets", "ordered": bool(ordered), "items": [_text(i, "列表项") for i in items]})

    def table(self, headers, rows, caption=None, col_widths=None):
        """col_widths 是各列宽度的比例（和不必是 100），不给就平分。单元格里的数字请先格式化成字符串，None 不接受。"""
        if not isinstance(headers, (list, tuple)) or not headers:
            raise ReportError("表格至少要有一列表头")
        headers = [_text(h, "表头") for h in headers]
        if not isinstance(rows, (list, tuple)):
            raise ReportError("rows 必须是行的列表")
        out = []
        for i, row in enumerate(rows):
            if not isinstance(row, (list, tuple)) or len(row) != len(headers):
                raise ReportError(f"第 {i + 1} 行的格数和表头的 {len(headers)} 列对不上")
            out.append([_text(c, f"第 {i + 1} 行的单元格", True) for c in row])
        if col_widths is not None and (len(col_widths) != len(headers) or not all(isinstance(w, (int, float)) and w > 0 for w in col_widths)):
            raise ReportError("col_widths 要和表头同样多，且都是正数")
        self._add({"type": "table", "headers": headers, "rows": out, "col_widths": list(col_widths) if col_widths else None,
                   "caption": _text(caption, "表题") if caption is not None else None})

    def figure(self, path, caption=None, width_pct=100):
        """path 是 run_python 工作目录里已生成的图片（png、jpg、svg）。"""
        if not isinstance(path, str) or os.path.splitext(path)[1].lower() not in _IMAGE_EXTENSIONS:
            raise ReportError(f"图片必须是 {'、'.join(_IMAGE_EXTENSIONS)} 文件")
        if not os.path.isfile(path):
            raise ReportError(f"找不到图片文件 {path}，先生成它再放进报告")
        if not isinstance(width_pct, (int, float)) or not 10 <= width_pct <= 100:
            raise ReportError("width_pct 必须在 10 到 100 之间")
        self._add({"type": "figure", "path": path, "width_pct": width_pct,
                   "caption": _text(caption, "图题") if caption is not None else None})

    def callout(self, text, tone="info"):
        if tone not in TONES:
            raise ReportError(f"提示框语气只能是 {'、'.join(TONES)}")
        self._add({"type": "callout", "tone": tone, "text": _text(text, "提示框")})

    def page_break(self):
        self._add({"type": "page_break"})

    def web_source(self, ref, title, url, site=None, date=None):
        """登记一条网页来源。ref 写成 "w3"，和正文里【w3「…」】的编号一致；标题、站点、日期、链接取自 web_search 的结果。"""
        if not isinstance(ref, str) or ref[:1] != "w" or not ref[1:].isdigit():
            raise ReportError('ref 要写成 "w3" 这样的编号')
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ReportError(f"{ref} 的链接必须是 http 或 https 地址")
        self.web_sources[ref] = {"title": _text(title, "网页标题"), "url": url,
                                 "site": site if site is None else _text(site, "站点"),
                                 "date": date if date is None else _text(date, "日期")}

    def source_notes(self, title, notes):
        """PDF 文末来源一节换个标题，并让指定的条目只写一句说明：notes 是 {(kind, 文件, 起, 止, 摘抄): 说明}，键与 cite_key 一致。
        没列在 notes 里的条目照常写位置和摘抄。"""
        self.source_view = {"title": _text(title, "来源一节的标题"), "notes": dict(notes)}

    def requote(self, no, new_quote):
        """交付被拒时按提示里的编号改摘抄：把清单第 no 条引用在全文里的摘抄换成 new_quote，不用重写整份报告。
        摘抄必须是原文里连续的一段。返回改了几处。"""
        entries = self.manifest()
        if not isinstance(no, int) or not 1 <= no <= len(entries):
            raise ReportError(f"清单里没有第 {no} 条，现有 1 到 {len(entries)} 条")
        if any(ch in _text(new_quote, "新摘抄") for ch in "【】「」"):
            raise ReportError("摘抄里不能有【】「」")
        key, hits = cite_key(entries[no - 1]), [0]
        pattern = re.compile("|".join((inline.FILE_CITATION, inline.WEB_CITATION, inline.QCC_CITATION)))

        def swap(cell):
            def one(m):
                seg = inline.parse(m.group(0))[0]
                if cite_key(seg) != key:
                    return m.group(0)
                hits[0] += 1
                raw = m.group(0)
                return raw[:raw.rindex("「") + 1] + new_quote + "」】"
            return pattern.sub(one, cell)

        def walk(value):
            if isinstance(value, list):
                return [walk(v) for v in value]
            return swap(value) if isinstance(value, str) else value

        for node in self.nodes:
            for field in _INLINE.get(node["type"], ()):
                if node.get(field) is not None:
                    node[field] = walk(node[field])
        return hits[0]

    def scenario(self, name, uscc, gaps=None):
        """声明这份报告属于哪个场景（如 "due-diligence"）、写的是哪家公司（18 位信用代码；境外主体没有信用代码，传空字符串，且 gaps 要覆盖每个必备维度）。声明后，交付前会核对该场景的必备维度：
        查过的要有引用，没查到的要在 gaps 里写明原因：{维度名: 原因}，例如 {"shareholders": "用户要求不查股权"}。"""
        gaps = {} if gaps is None else gaps
        if not isinstance(uscc, str) or (uscc != "" and not _USCC.match(uscc)):
            raise ReportError("uscc 必须是 18 位统一社会信用代码，境外主体传空字符串")
        if not isinstance(gaps, dict):
            raise ReportError("gaps 必须是 {维度名: 原因} 的字典")
        self.scenario_decl = {"name": _text(name, "场景名"), "uscc": uscc,
                              "gaps": {_text(k, "gaps 的维度名"): _text(v, f"{k} 的原因") for k, v in gaps.items()}}

    # ---- 引用清单 ----

    def segments(self):
        """按文中顺序逐段给出每个带行内标记的字段解析后的片段列表。"""
        for node in self.nodes:
            for field in _INLINE.get(node["type"], ()):
                for cell in _flatten(node.get(field)):
                    yield inline.parse(cell)

    def manifest(self):
        """全文的引用，按第一次出现的顺序编号，相同的引用共用一个号。网页引用必须已登记来源，否则报错。"""
        seen, entries = {}, []
        for segments in self.segments():
            for seg in segments:
                if seg["t"] != "cite" or cite_key(seg) in seen:
                    continue
                entry = {"no": len(entries) + 1, **{k: seg[k] for k in ("kind", "file", "first", "last", "quote")}}
                if seg["kind"] == "w":
                    source = self.web_sources.get(f"w{seg['first']}")
                    if source is None:
                        raise ReportError(f"正文引用了 w{seg['first']}，但没有登记它的来源，先调用 web_source 登记标题和链接")
                    entry.update(title=source["title"], url=source["url"], site=source["site"], date=source["date"])
                seen[cite_key(seg)] = entry["no"]
                entries.append(entry)
        return entries

    def embedded(self):
        """嵌进 PDF、docx 的清单：引用，加上场景声明（没声明是 None）。交付前由宿主核对。"""
        return {"citations": self.manifest(), "scenario": self.scenario_decl}

    def validate(self):
        if not any(n["type"] != "cover" for n in self.nodes):
            raise ReportError("报告里还没有内容")
        self.manifest()

    # ---- 输出 ----

    def save_pdf(self, path, theme_path=None):
        """渲染成 PDF。返回页数、字节数和引用条数。字体、内容、引用有问题会直接报错。"""
        from eva_report import pdf
        return pdf.save(self, path, theme_path)

    def save_docx(self, path, theme_path=None):
        """渲染成 Word 文档，版式读同一份主题。图片不支持 svg。"""
        from eva_report import word
        return word.save(self, path, theme_path)

    def save_markdown(self, path):
        from eva_report import markdown
        return markdown.save(self, path)

    # ---- 存取 ----

    def to_dict(self):
        return {"title": self.title, "nodes": self.nodes, "web_sources": self.web_sources, "scenario": self.scenario_decl}

    def save_json(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)

    @classmethod
    def load_json(cls, path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            report = cls(data["title"])
            report.nodes, report.web_sources, report.scenario_decl = data["nodes"], data["web_sources"], data["scenario"]
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise ReportError(f"读不回报告文件 {path}：{e}") from e
        report.validate()
        return report


def cite_key(seg):
    """区分两条引用是不是同一条：同文件同位置同摘抄才共用一个号。"""
    return (seg["kind"], seg["file"], seg["first"], seg["last"], seg["quote"])
