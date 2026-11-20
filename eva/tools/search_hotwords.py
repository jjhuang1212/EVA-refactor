"""search_hotwords：在公共热词表（上市公司名、行业术语）里按读音找候选，给转写纠错用。

转写的错字多是同音或近音字（"宁得时代"→"宁德时代"），所以按读音比：把查询词的每个字转成拼音（忽略声调，
翘舌/平舌、前后鼻音视为同音），在查询词上滑动一个热词长度的窗口，对得上的字数占比达到下限的就是候选。
查公共表,以及当前租户自己确认过的写法(confirm_term 记下的,排在前面,常被听成的错写也能直接命中);候选只是线索，改不改由模型结合上下文判断。没有候选是空列表，不是错误。
"""
import json
import time
from collections import defaultdict
from typing import Callable, Dict, List, Tuple

from pypinyin import Style, lazy_pinyin

from eva.contracts.tooling import ToolContext
from eva.contracts.vocab import Concurrency
from eva.storage import db
from eva.storage.hotwords import Hotwords
from eva.storage.tenant_terms import TenantTerms
from eva.tools.base import Tool
from eva.tools.config import SearchHotwordsConfig

_FINALS = (("ang", "an"), ("eng", "en"), ("ing", "in"))
_INITIALS = (("zh", "z"), ("ch", "c"), ("sh", "s"))
_MIN_QUERY_CHARS = 2
_TENANT_WEIGHT = 1000.0  # 本租户确认过的写法同分时排在公共热词前面

_DESCRIPTION = (
    "在热词表（公共的，加上本租户确认过的写法）里找读音相近的公司名和行业术语，用来判断转写稿里疑似听错的词该改成什么。"
    "terms 里放转写稿中疑似有误的词或短语（可以带前后一两个字，不要整段），每个词返回读音相近的候选。"
    "match=exact 说明转写稿里这个写法已经和热词一致，不用改；phonetic 是读音相近；contains 是查询词只是热词的一部分；known_mishearing 是用户此前确认过这个错写就是它。source=tenant 的是本租户用户确认过的，比公共表更可信。"
    "候选只是线索：要结合上下文判断，读音相近但上下文说的是别的东西就不要改。没有候选时 matches 为空，不是错误。"
)
_PARAMETERS = {
    "type": "object",
    "properties": {"terms": {"type": "array", "items": {"type": "string", "minLength": 2}, "minItems": 1, "uniqueItems": True,
                             "description": "疑似有误的词或短语"}},
    "required": ["terms"],
    "additionalProperties": False,
}


def _syllable(py: str) -> str:
    for a, b in _INITIALS:
        if py.startswith(a):
            py = b + py[len(a):]
            break
    for a, b in _FINALS:
        if py.endswith(a):
            return py[:-len(a)] + b
    return py


def sounds(text: str) -> Tuple[str, ...]:
    """每个字一个音节；非汉字（数字、字母）原样，按字符比。"""
    return tuple(_syllable(p) for p in lazy_pinyin(text, style=Style.NORMAL, errors=lambda s: list(s)))


class _Index:
    def __init__(self, rows: List[dict]):
        self.rows = [(r, sounds(r["term"])) for r in rows if len(r["term"]) >= _MIN_QUERY_CHARS]
        self.by_len: Dict[int, list] = defaultdict(list)
        for entry in self.rows:
            self.by_len[len(entry[0]["term"])].append(entry)

    def search(self, query: str, min_similarity: float, limit: int) -> List[dict]:
        q_sounds, found = sounds(query), {}
        for length, entries in self.by_len.items():
            if length > len(query):
                continue
            allowed = length - min_similarity * length  # 允许对不上的字数
            for start in range(len(query) - length + 1):
                window = q_sounds[start:start + length]
                for row, term_sounds in entries:
                    miss = 0
                    for a, b in zip(window, term_sounds):
                        if a != b:
                            miss += 1
                            if miss > allowed:
                                break
                    else:
                        score = (length - miss) / length
                        kind = "exact" if query[start:start + length] == row["term"] else "phonetic"
                        if kind == "exact" and row.get("alias_of"):
                            kind = "known_mishearing"
                        shown = row.get("alias_of") or row["term"]
                        if shown not in found or score > found[shown][0]:
                            found[shown] = (score, kind, query[start:start + length], row)
        for entry in self.rows:  # 查询词只是热词的一部分，如"宁德"对"宁德时代"
            row = entry[0]
            if not row.get("alias_of") and len(query) < len(row["term"]) and query in row["term"] and row["term"] not in found:
                found[row["term"]] = (len(query) / len(row["term"]), "contains", query, row)
        ranked = sorted(found.items(), key=lambda f: (f[1][1] not in ("exact", "known_mishearing"), -f[1][0], -f[1][3]["weight"], f[0]))[:limit]
        return [{"term": shown, "match": kind, "score": round(score, 2), "heard_as": heard, "kind": r["source_kind"],
                 "industry": r["industry"], "source": r.get("source", "public")} for shown, (score, kind, heard, r) in ranked]


def make_search_hotwords(cfg: SearchHotwordsConfig, open_tx: Callable = db.transaction, clock: Callable[[], float] = time.monotonic) -> Tool:
    cache = {"at": None, "index": None}

    def index(tenant_id: str) -> _Index:
        if cache["index"] is None or clock() - cache["at"] > cfg.cache_seconds:
            with open_tx() as conn:  # 公共表，所有租户同一份；缓存不带租户
                cache["index"], cache["at"] = _Index(Hotwords(conn, tenant_id).all()), clock()
        return cache["index"]

    def tenant_index(tenant_id: str) -> _Index:
        """本租户确认过的写法,量小且随时会变,每次现查不缓存。写错的 heard_as 另成一行,指向正确写法。"""
        with open_tx() as conn:
            mine = TenantTerms(conn, tenant_id).all()
        rows = []
        for m in mine:
            base = {"weight": _TENANT_WEIGHT, "source_kind": m["kind"], "industry": None, "source": "tenant"}
            rows.append({**base, "term": m["term"]})
            if m["heard_as"]:
                rows.append({**base, "term": m["heard_as"], "alias_of": m["term"]})
        return _Index(rows)

    def search(public: _Index, mine: _Index, text: str) -> List[dict]:
        own = mine.search(text, cfg.min_similarity, cfg.max_per_term)
        taken = {m["term"] for m in own}
        rest = [m for m in public.search(text, cfg.min_similarity, cfg.max_per_term) if m["term"] not in taken]
        return (own + rest)[:cfg.max_per_term]

    def run(args: dict, ctx: ToolContext) -> str:
        terms = args["terms"][:cfg.max_terms]
        public, mine = index(ctx.tenant_id), tenant_index(ctx.tenant_id)
        out = {"results": [{"term": t, "matches": search(public, mine, t)} for t in terms]}
        if len(args["terms"]) > len(terms):
            out["skipped"] = args["terms"][len(terms):]
        return json.dumps(out, ensure_ascii=False)

    return Tool("search_hotwords", _DESCRIPTION, _PARAMETERS, run, concurrency=Concurrency.SHARED)
