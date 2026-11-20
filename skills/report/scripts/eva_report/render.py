"""PDF 和 Markdown 两种输出共用的部分：引用编号、来源条目的文字。"""
from eva_report.tree import cite_key


def cite_numbers(report):
    """引用 -> 清单编号。渲染一份报告只算一次。"""
    return {cite_key(e): e["no"] for e in report.manifest()}


def location(entry):
    """文件引用的位置：块12、块12-14、行3-5。企业数据和网页引用没有位置。"""
    if entry["kind"] == "w":
        return ""
    if entry["kind"] == "q":
        return ""
    unit = "块" if entry["kind"] == "b" else "行"
    return f"{unit}{entry['first']}" + (f"-{entry['last']}" if entry["last"] != entry["first"] else "")


def source_title(entry):
    """来源条目的名称：文件名，企业数据，或网页标题加站点、日期。"""
    if entry["kind"] == "q":
        return "企业数据（企查查）"
    if entry["kind"] != "w":
        return entry["file"]
    return " · ".join(x for x in (entry["title"], entry.get("site"), entry.get("date")) if x)

def source_line(entry):
    """来源条目里名称加位置的那一段；企业数据引用的编号只在本次查询里有意义，不给用户看。"""
    return " · ".join(x for x in (source_title(entry), location(entry)) if x)
