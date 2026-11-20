"""主题的读取与校验。字段缺了或不合法就报错，不用默认值顶替：缺了配色的报告会悄悄变成另一个样子。"""
import os
import re

import yaml

from eva_report.errors import ReportError
from eva_report.inline import MARKS

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "theme.yaml")
_HEX = re.compile(r"#[0-9a-fA-F]{6}")
_COLORS = ("text", "brand", "muted", "faint", "line", "zebra", "link")
_TONES = ("info", "ok", "warn", "bad")
_SIZES = ("body", "h1", "h2", "h3", "title", "subtitle", "small", "footer")
_MARGINS = ("top", "right", "bottom", "left")
_PAGE_SIZES = ("A4", "A3", "Letter")


def _need(cond, message):
    if not cond:
        raise ReportError(f"主题不合法：{message}")


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def load_theme(path=None):
    path = path or DEFAULT_PATH
    try:
        with open(path, encoding="utf-8") as f:
            theme = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        raise ReportError(f"读不到或解析不了主题文件 {path}：{e}") from e
    _need(isinstance(theme, dict), "顶层必须是映射")
    fonts = (theme.get("fonts") or {}).get("sans")
    _need(isinstance(fonts, list) and fonts and all(isinstance(x, str) and x.strip() for x in fonts), "fonts.sans 必须是非空的字体名列表")
    docx_font = theme["fonts"].get("docx")
    _need(isinstance(docx_font, str) and docx_font.strip(), "fonts.docx 必须是字体名")
    colors = theme.get("colors")
    _need(isinstance(colors, dict) and all(isinstance(colors.get(k), str) and _HEX.fullmatch(colors[k]) for k in _COLORS),
          f"colors 必须给齐 {'、'.join(_COLORS)}，值是 #rrggbb")
    tones = theme.get("tones")
    _need(isinstance(tones, dict) and all(
        isinstance(tones.get(t), dict) and all(isinstance(tones[t].get(k), str) and _HEX.fullmatch(tones[t][k]) for k in ("fg", "bg"))
        for t in _TONES), f"tones 必须给齐 {'、'.join(_TONES)}，每个有 fg 和 bg（#rrggbb）")
    marks = theme.get("marks")
    _need(isinstance(marks, dict) and all(isinstance(marks.get(k), str) and _HEX.fullmatch(marks[k]) for k in MARKS),
          f"marks 必须给齐 {'、'.join(MARKS)}，值是底色 #rrggbb")
    sizes = theme.get("sizes_pt")
    _need(isinstance(sizes, dict) and all(_number(sizes.get(k)) for k in _SIZES), f"sizes_pt 必须给齐 {'、'.join(_SIZES)}，值是正数")
    _need(_number(theme.get("line_height")), "line_height 必须是正数")
    page = theme.get("page")
    _need(isinstance(page, dict) and page.get("size") in _PAGE_SIZES, f"page.size 必须是 {'、'.join(_PAGE_SIZES)} 之一")
    _need(isinstance(page.get("margin_mm"), dict) and all(_number(page["margin_mm"].get(k)) for k in _MARGINS),
          f"page.margin_mm 必须给齐 {'、'.join(_MARGINS)}，值是正数")
    return theme
