"""文档树 -> Markdown。引用变成 [n] 角标，文末列出来源，网页来源带链接。"""
import os

from eva_report import inline as markup
from eva_report.render import cite_numbers, source_line, source_title
from eva_report.tree import cite_key


def _cell(text):
    return text.replace("|", "\\|").replace("\n", "<br>")


def _inline(text, numbers):
    out = []
    for seg in markup.parse(text):
        if seg["t"] == "text":
            out.append(seg["text"])
        elif seg["t"] == "bold":
            out.append(f"**{seg['text']}**")
        elif seg["t"] == "mark":
            out.append(f"**{seg['text']}**")
        elif seg["t"] == "link":
            out.append(f"[{seg['text']}]({seg['url']})")
        else:
            out.append(f"[{numbers[cite_key(seg)]}]")
    return "".join(out)


def render(report):
    numbers, lines, counter = cite_numbers(report), [], 0
    for n in report.nodes:
        kind = n["type"]
        if kind == "cover":
            lines += [f"# {report.title}", ""]
            if n.get("eyebrow"):
                lines += [n["eyebrow"], ""]
            if n["subtitle"]:
                lines += [f"{n['subtitle']}", ""]
            lines += [f"- {k}：{v}" for k, v in n["meta"]] + ([""] if n["meta"] else [])
        elif kind == "heading":
            if n["level"] == 1:
                counter += 1
            lines += [f"{'#' * (n['level'] + 1)} {n['text']}", ""]
        elif kind == "paragraph":
            lines += [_inline(n["text"], numbers), ""]
        elif kind == "bullets":
            lines += [f"{i + 1}. {_inline(t, numbers)}" if n["ordered"] else f"- {_inline(t, numbers)}" for i, t in enumerate(n["items"])] + [""]
        elif kind == "table":
            if n["caption"]:
                lines += [f"**{_inline(n['caption'], numbers)}**", ""]
            lines += ["| " + " | ".join(_cell(h) for h in n["headers"]) + " |", "|" + " --- |" * len(n["headers"])]
            lines += ["| " + " | ".join(_cell(_inline(c, numbers)) for c in row) + " |" for row in n["rows"]] + [""]
        elif kind == "figure":
            lines += [f"![{_inline(n['caption'], numbers) if n['caption'] else os.path.basename(n['path'])}]({n['path']})", ""]
        elif kind == "callout":
            lines += [f"> {_inline(n['text'], numbers)}".replace("\n", "\n> "), ""]
    entries = report.manifest()
    if entries:
        lines += ["## 来源", ""]
        for e in entries:
            where = f"[{source_title(e)}]({e['url']})" if e["kind"] == "w" else source_line(e)
            lines.append(f"{e['no']}. {where} 「{e['quote']}」")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def save(report, path):
    report.validate()
    text = render(report)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return {"bytes": len(text.encode("utf-8")), "citations": len(report.manifest())}
