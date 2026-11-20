"""出处校验：模型想结束回答时，核对回答里每条引用，不通过就把原因注入让它重读。
交付文件前同样核对：用报告组件库生成的 PDF 带着自己的引用清单，清单里每条都要通过同一套核对，网页引用的链接还要和搜索结果里存的一致。

核对的是真伪，不是读没读过：文件必须是本租户的，块号或行号必须存在，摘抄必须真的出现在所引的块或行里。
网页引用核对的是本会话里 web_search 返回过的结果：编号必须存在，摘抄必须出现在那一条的标题或正文里。
企业数据引用核对的是本会话里 query_company 返回过的记录：编号必须存在，摘抄必须出现在那一条里。
报告声明了场景（Report.scenario）时，引用核对通过后再核对该场景的必备维度覆盖，见 hooks/coverage.py。
回答里没有引用时，只有本 turn 用过读文件、查企业数据、联网搜索这类工具、回答又够长，才拦一次；闲聊和澄清不需要引用。
"""
import io
import json
import re
import unicodedata
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

from eva.common.manifest import read_manifest
from eva.contracts import citation
from eva.contracts.citation import Citation
from eva.contracts.document import Parsed
from eva.contracts.tooling import ToolCall, ToolContext
from eva.contracts.vocab import BlockKind, EventType, HookDecision
from eva.hooks import coverage
from eva.hooks.base import Hook, HookResult, StopResult
from eva.hooks.config import CitationConfig
from eva.storage import db
from eva.storage.objects import ObjectStore, Objects
from eva.storage.qcc_queries import QccQueries
from eva.storage.qcc_results import QccResults
from eva.storage.transcript import Transcript
from eva.storage.web_results import WebResults

_HEAD = "回答里有引用没有通过核对。请重新读相关位置，把摘抄改成原文里真实存在的文字，或删掉无法核实的说法，然后再回答："
_UNCITED_HEAD = "回答里有来自文件或查询结果的内容，但一条引用也没有。"
_UNCITED = (_UNCITED_HEAD + "请给每个来自文件、企业数据或网页的事实补上引用；确实找不到依据的说法，改写成明确说明没有找到，不要保留。"
            "引用格式：")
_REPORT_HEAD = ("报告里有引用没有通过核对，文件没有交付。请重新读相关位置，改正后重新生成报告文件，再交付。"
                "不用重写整份脚本：Report.load_json 读回保存的报告，按下面方括号里的编号调用 r.requote(编号, 新摘抄) 改摘抄，再重新保存、交付：")
_COVERAGE_HEAD = "报告声明了场景，但必备维度没有覆盖齐，文件没有交付。请按下面补查或补引用，或在 gaps 里写明原因，改正后重新生成报告文件，再交付："
_NO_MANIFEST = ("文件没有交付：本会话查过文件、企业数据或网页，PDF 和 docx 报告必须用 skills/report 的组件库（save_pdf、save_docx）生成，"
                "里面带的引用清单才能核对出处，手写的文件无法核对。请改用组件库重新生成再交付。")
_UNDECLARED = ("文件没有交付：本会话读过 {name} skill，报告要用 r.scenario(\"{name}\", 信用代码, gaps) 声明场景，交付前才能核对必备维度有没有覆盖。"
               "声明后重新生成报告文件，再交付。")
_PREVIEW_CHARS = 60
_MIN_PREFIX = 4  # 摘抄开头至少对上这么多字，才值得告诉模型从哪里开始对不上


def _norm(text: str) -> str:
    """全半角统一、去掉所有空白：pdf 抽出的文字常在不该断的地方换行或加空格。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _diverge(quote: str, text: str) -> str:
    """摘抄（已规整）从头起能在原文里连续对上多长，以及对不上之处原文接着是什么。对上不到 _MIN_PREFIX 个字返回空串。"""
    lo, hi = 0, len(quote)
    while lo < hi:  # 前缀越短越容易对上，可以二分
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if quote[:mid] in text else (lo, mid - 1)
    if lo < _MIN_PREFIX:
        return ""
    at = text.find(quote[:lo]) + lo
    return (f"摘抄前 {lo} 个字对得上，从「{quote[lo:lo + 12]}」起对不上，原文这里接着是「{text[at:at + _PREVIEW_CHARS]}」。"
            "摘抄必须是原文里连续的一段：不要跳过中间的字段，不要改写，只取支撑这个事实的一个字段就够")


def _check_link(rows: List[dict], ref_no: int, url: str) -> Optional[str]:
    row = next((r for r in rows if r["ref_no"] == ref_no), None)
    if row is None or row["link"].strip() != url.strip():
        return f"链接和搜索结果里 w{ref_no} 的不一致，网页链接只能用 web_search 返回的"
    return None


def _is_report_format(data: bytes) -> bool:
    """PDF，或含 word/document.xml 的 zip（docx）。别的 zip（xlsx、pptx）不算。"""
    if data.startswith(b"%PDF"):
        return True
    if not data.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return "word/document.xml" in z.namelist()
    except zipfile.BadZipFile:
        return False


class CitationHook(Hook):
    name = "citations"

    def __init__(self, store: ObjectStore, cfg: CitationConfig, max_file_bytes: int,
                 open_tx: Callable = db.transaction, max_deliver_bytes: Optional[int] = None,
                 scenarios: Optional[Dict[str, Tuple[str, ...]]] = None):
        self._store, self._cfg, self._max_bytes, self._open_tx = store, cfg, max_file_bytes, open_tx
        self._scenarios = scenarios or {}  # 场景名 -> 必备维度，见 configs/hooks.yaml
        self._max_deliver = max_deliver_bytes or max_file_bytes  # 报告 PDF 常比可读文件大，交付核对用单独的上限

    def stop(self, ctx: ToolContext, final_text: str) -> StopResult:
        found, malformed = citation.find(final_text)
        if not found and not malformed:
            return self._uncited(ctx, final_text)
        problems = [f"{raw}：格式不对，应写成 {citation.FORMAT_HELP}" for raw in malformed]
        with self._open_tx() as conn:
            objects, web, qcc = Objects(conn, ctx.tenant_id, self._store), WebResults(conn, ctx.tenant_id), QccResults(conn, ctx.tenant_id)
            loaded = {}
            for c in found:
                why = self._check_any(objects, web, qcc, ctx.session_id, loaded, c)
                if why:
                    problems.append(f"{c.raw}：{why}")
        return StopResult(True, self._report(_HEAD, problems)) if problems else StopResult()

    def _uncited(self, ctx: ToolContext, final_text: str) -> StopResult:
        """回答没有任何引用：本 turn 用过取证类工具、回答够长、且此前没拦过，才拦。状态都在日志里，重启后不会重复拦。
        用过免查工具（如录音转写稿导出）的 turn 不拦：回答是交付说明，内容在交付的文件里。"""
        if not self._cfg.uncited_tools or len(final_text.strip()) < self._cfg.uncited_min_chars:
            return StopResult()
        with self._open_tx() as conn:
            events = Transcript(conn, ctx.tenant_id).current_turn(ctx.session_id)
        if any(e["event_type"] == EventType.USER_MESSAGE.value and _UNCITED_HEAD in str(e["payload"].get("text"))
               for e in events):
            return StopResult()
        names = {e["payload"]["call_id"]: e["payload"]["name"] for e in events if e["event_type"] == EventType.TOOL_CALL.value}
        def called(tools):
            return any(e["event_type"] == EventType.TOOL_RESULT.value and not e["payload"].get("is_error")
                       and names.get(e["payload"].get("call_id")) in tools for e in events)

        if called(self._cfg.uncited_exempt_tools):
            return StopResult()
        delivered = [json.loads(e["payload"]["content"])["filename"] for e in events
                     if e["event_type"] == EventType.TOOL_RESULT.value and not e["payload"].get("is_error")
                     and names.get(e["payload"].get("call_id")) == "deliver_file"]
        if any(re.search(x, f) for x in self._cfg.uncited_exempt_files for f in delivered):
            return StopResult()
        used = called(self._cfg.uncited_tools)
        return StopResult(True, _UNCITED + citation.FORMAT_HELP + "；" + citation.WEB_FORMAT_HELP + "；"
                          + citation.QCC_FORMAT_HELP) if used else StopResult()

    def pre_tool(self, ctx: ToolContext, call: ToolCall) -> HookResult:
        """deliver_file 之前核对 PDF 或 docx 里带的引用清单。没有清单的文件不是用组件库生成的报告，放行；清单坏了或打不开的 PDF 不放行。"""
        if call.name != "deliver_file":
            return HookResult()
        with self._open_tx() as conn:
            objects, web, qcc = Objects(conn, ctx.tenant_id, self._store), WebResults(conn, ctx.tenant_id), QccResults(conn, ctx.tenant_id)
            _, data = objects.read_original(call.arguments["file_id"], self._max_deliver)
            entries, scenario, problem = read_manifest(data)
            if problem:
                return HookResult(HookDecision.DENY, problem)
            if entries is None:
                return self._without_manifest(conn, ctx, data)
            problems, loaded = [], {}
            for e in entries or []:
                c = Citation(e["file"], e["kind"], e["first"], e["last"], e["quote"], f"[{e['no']}]")
                why = self._check_any(objects, web, qcc, ctx.session_id, loaded, c)
                if why is None and c.kind == citation.KIND_WEB:
                    why = _check_link(loaded["web"], c.first, e["url"])
                if why:
                    problems.append(f"[{e['no']}] " + (f"{e['file']}：" if e["file"] else "") + why)
            if problems:
                return HookResult(HookDecision.DENY, self._report(_REPORT_HEAD, problems))
            if scenario is None:
                return self._undeclared(conn, ctx)
            required = self._scenarios.get(scenario["name"])
            if required is None:
                return HookResult(HookDecision.DENY, f"{_COVERAGE_HEAD}\n- 不认识的场景「{scenario['name']}」，可用的：{'、'.join(self._scenarios) or '无'}")
            if "qcc" not in loaded:
                loaded["qcc"] = qcc.list(ctx.session_id)
            gaps = coverage.check(required, scenario, entries, loaded["qcc"],
                                  QccQueries(conn, ctx.tenant_id).outcomes(ctx.session_id, scenario["uscc"]))
        return HookResult(HookDecision.DENY, self._report(_COVERAGE_HEAD, gaps)) if gaps else HookResult()

    @staticmethod
    def _succeeded_calls(conn, ctx: ToolContext) -> List[Tuple[str, object]]:
        """本会话里没报错的工具调用（名字, 参数）。"""
        events = Transcript(conn, ctx.tenant_id).read(ctx.session_id)
        failed = {e["payload"]["call_id"] for e in events if e["event_type"] == EventType.TOOL_RESULT.value and e["payload"].get("is_error")}
        return [(e["payload"]["name"], e["payload"].get("arguments")) for e in events
                if e["event_type"] == EventType.TOOL_CALL.value and e["payload"]["call_id"] not in failed]

    def _without_manifest(self, conn, ctx: ToolContext, data: bytes) -> HookResult:
        """PDF、docx 没带引用清单：本会话查过文件、企业数据或网页，就说明里面的事实该有出处，手写的文件无法核对，不交付。
        没查过这类内容（闲聊、回传用户自己的文件）的不管。"""
        if not _is_report_format(data) or not {n for n, _ in self._succeeded_calls(conn, ctx)} & set(self._cfg.uncited_tools):
            return HookResult()
        return HookResult(HookDecision.DENY, _NO_MANIFEST)

    def _undeclared(self, conn, ctx: ToolContext) -> HookResult:
        """报告没声明场景：本会话读过某个场景的 skill，就必须声明，否则核对不了必备维度。场景名与 skill 名相同。"""
        read = {a.get("name") for n, a in self._succeeded_calls(conn, ctx) if n == "load_skill" and isinstance(a, dict)}
        active = [s for s in self._scenarios if s in read]
        if not active:
            return HookResult()
        return HookResult(HookDecision.DENY, _UNDECLARED.format(name=active[0]))

    def _report(self, head: str, problems: List[str]) -> str:
        shown = problems[:self._cfg.max_reported]
        more = [f"……还有 {len(problems) - len(shown)} 条没通过"] if len(problems) > len(shown) else []
        return "\n".join([head] + [f"- {p}" for p in shown] + more)

    def _check(self, objects: Objects, loaded: dict, c: Citation) -> Optional[str]:
        """返回不通过的原因；通过返回 None。"""
        quote = _norm(c.quote)
        if len(quote) < self._cfg.min_quote_chars:
            return f"摘抄太短，至少 {self._cfg.min_quote_chars} 个字符（不含空白），请摘一整句或一个完整数据"
        if c.last < c.first:
            return "起止位置写反了"
        if c.file not in loaded:
            loaded[c.file] = objects.available_named(c.file)
        rows = loaded[c.file]
        if not rows:
            return f"本租户没有名为「{c.file}」的文件，文件名要和上传时一致"
        if len(rows) > 1:  # 登记时已保证文件名不重复；走到这里是并发或历史数据，宁可不过也不猜是哪一份
            return f"本租户有 {len(rows)} 个同名文件，引用指不到唯一一份"
        return self._check_in(objects, rows[0], c, quote)

    def _check_any(self, objects: Objects, web: WebResults, qcc: QccResults, session_id: str, loaded: dict, c: Citation) -> Optional[str]:
        if c.kind == citation.KIND_WEB:
            return self._check_web(web, session_id, loaded, c)
        if c.kind == citation.KIND_QCC:
            return self._check_qcc(qcc, session_id, loaded, c)
        return self._check(objects, loaded, c)

    def _check_qcc(self, qcc: QccResults, session_id: str, loaded: dict, c: Citation) -> Optional[str]:
        quote = _norm(c.quote)
        if len(quote) < self._cfg.min_quote_chars:
            return f"摘抄太短，至少 {self._cfg.min_quote_chars} 个字符（不含空白），请摘一整句或一个完整数据"
        if "qcc" not in loaded:  # 一次回答里的几条企业数据引用共用一次查询
            loaded["qcc"] = qcc.list(session_id)
        by_no = {r["ref_no"]: r for r in loaded["qcc"]}
        row = by_no.get(c.first)
        if not by_no:
            return (f"本会话还没有 q 编号（query_company 没返回过记录），q{c.first} 不存在。如果这句话的事实来自 resolve_entity 返回的候选信息，"
                    "直接去掉这个引用；确实要有企业数据的出处，先调用 query_company 查对应维度，再引用它返回的编号")
        if row is None:
            return f"本会话的企业数据里没有 q{c.first}，只能引用 query_company 返回过的编号"
        if quote in _norm(row["content"]):
            return None
        where = [f"q{n}" for n, r in by_no.items() if quote in _norm(r["content"])][:3]
        if where:
            return f"q{c.first} 里没有这段原文，这段原文出现在 {'、'.join(where)}"
        return (f"q{c.first} 里没有这段原文，所有企业数据里也找不到。"
                + (_diverge(quote, _norm(row["content"])) or f"q{c.first} 是：{row['content'][:_PREVIEW_CHARS]}"))

    def _check_web(self, web: WebResults, session_id: str, loaded: dict, c: Citation) -> Optional[str]:
        quote = _norm(c.quote)
        if len(quote) < self._cfg.min_quote_chars:
            return f"摘抄太短，至少 {self._cfg.min_quote_chars} 个字符（不含空白），请摘一整句或一个完整数据"
        if "web" not in loaded:  # 一次回答里的几条网页引用共用一次查询
            loaded["web"] = web.list(session_id)
        by_no = {r["ref_no"]: r for r in loaded["web"]}
        row = by_no.get(c.first)
        if not by_no:
            return f"本会话还没有 w 编号（web_search 没返回过结果），w{c.first} 不存在。先调用 web_search，再引用它返回的编号；没有搜过就去掉这个引用"
        if row is None:
            return f"本会话的搜索结果里没有 w{c.first}，只能引用 web_search 返回过的编号"
        if quote in _norm(row["title"]) or quote in _norm(row["content"]):
            return None
        where = [f"w{n}" for n, r in by_no.items() if quote in _norm(r["title"]) or quote in _norm(r["content"])][:3]
        if where:
            return f"w{c.first} 里没有这段原文，这段原文出现在 {'、'.join(where)}"
        return f"w{c.first} 里没有这段原文，所有搜索结果里也找不到；w{c.first} 的标题是：{row['title'][:_PREVIEW_CHARS]}"

    def _check_in(self, objects: Objects, row: dict, c: Citation, quote: str) -> Optional[str]:
        if c.kind == "b":
            raw = objects.read_latest_parsed(row["file_id"])
            if raw is None:
                return "这份文件还没有按块读取过，纯文本文件请用行号，其他文件先用 read_file 读"
            parsed = Parsed.from_dict(json.loads(raw))
            return self._check_blocks(parsed.blocks, c, quote)
        _, data = objects.read_original(row["file_id"], self._max_bytes)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return "这份文件不是纯文本，不能按行号引用，请用块号"
        return self._check_lines(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), c, quote)

    @staticmethod
    def _check_blocks(blocks: tuple, c: Citation, quote: str) -> Optional[str]:
        by_ord = {b.ord: b for b in blocks}
        missing = [n for n in range(c.first, c.last + 1) if n not in by_ord]
        if missing:
            return f"没有 b{missing[0]} 这个块（共 {len(blocks)} 个块，b0 到 b{len(blocks) - 1}）"
        seen = [n for n in range(c.first, c.last + 1) if by_ord[n].kind is BlockKind.VISION]
        if seen:
            return f"b{seen[0]} 是看图得到的描述，不是文件里的原文，不能作为引用依据；这一块的内容不要写【】引用，在正文里说明来自看图，请用户核对"
        if quote in "".join(_norm(by_ord[n].text) for n in range(c.first, c.last + 1)):
            return None
        where = [f"b{b.ord}" for b in blocks if b.kind is not BlockKind.VISION and quote in _norm(b.text)][:3]
        if where:
            return f"所引的块里没有这段原文，这段原文出现在 {'、'.join(where)}"
        return f"所引的块里没有这段原文，整份文件里也找不到；b{c.first} 开头是：{by_ord[c.first].text[:_PREVIEW_CHARS]}"

    @staticmethod
    def _check_lines(lines: List[str], c: Citation, quote: str) -> Optional[str]:
        if c.first < 1 or c.last > len(lines):
            return f"没有第 {c.first}-{c.last} 行（共 {len(lines)} 行）"
        if quote in "".join(_norm(x) for x in lines[c.first - 1:c.last]):
            return None
        whole = "".join(_norm(x) for x in lines)
        at = whole.find(quote)
        if at < 0:
            return f"所引的行里没有这段原文，整份文件里也找不到；第 {c.first} 行开头是：{lines[c.first - 1][:_PREVIEW_CHARS]}"
        first = last = None
        offset = 0
        for number, line in enumerate(lines, start=1):  # 引文可能跨行，按去掉空白后的累计偏移找它落在哪几行
            size = len(_norm(line))
            if first is None and at < offset + size:
                first = number
            if first is not None and at + len(quote) <= offset + size:
                last = number
                break
            offset += size
        return f"所引的行里没有这段原文，这段原文出现在第 {first}-{last} 行"
