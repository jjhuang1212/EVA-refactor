"""场景报告的维度覆盖核对：模型自己决定查哪些、查多深，这里只卡"漏维度"。
必备维度逐项过：
- 查到记录的：报告里要引用至少一条该公司、该维度的 q 编号；
- 确认没有记录的：放行（"没有记录"没有 q 编号可引）；
- 没查、没匹配上、查失败的：报告声明的 gaps 里要写明原因。
"""
from typing import Dict, Iterable, List

from eva.contracts.vocab import QccOutcome

_WHY_MISSING = {None: "本会话没有查过", QccOutcome.UNMATCHED: "查询没有匹配上", QccOutcome.FAILED: "查询失败"}


def check(required: Iterable[str], scenario: dict, entries: List[dict], rows: List[dict], outcomes: Dict[str, QccOutcome]) -> List[str]:
    """返回没通过的说明，空表示通过。rows 是本会话的企业数据记录，outcomes 是这家公司各维度的查询结果。"""
    cited = {e["first"] for e in entries if e["kind"] == "q"}
    covered = {(r["uscc"], r["dimension"]) for r in rows if r["ref_no"] in cited}
    problems = []
    if not scenario["uscc"] and cited:
        problems.append("信用代码为空表示境外主体，不应引用企业数据的 q 编号；有信用代码的公司请填上信用代码")
    for dim in required:
        outcome = outcomes.get(dim)
        if outcome is QccOutcome.NONE:
            continue
        if outcome is QccOutcome.RECORDS:
            if (scenario["uscc"], dim) not in covered:
                problems.append(f"{dim}：查到了记录，但报告里没有引用它的 q 编号")
        elif not scenario["gaps"].get(dim):
            problems.append(f"{dim}：{_WHY_MISSING[outcome]}，请补查；确实不查或查不到，就在 Report.scenario 的 gaps 里写明原因")
    return problems
