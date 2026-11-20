"""时间线的终端文本渲染。纯函数：吃 timeline.build 的结果，吐字符串，不读库。"""
import json
from typing import List

_PREVIEW = 400  # 默认每段正文最多显示的字数；--full 不截


def render(tl: dict, full: bool = False) -> str:
    out = [f"会话 {tl['session_id']}  租户 {tl['tenant_id']}  共 {len(tl['items'])} 项", ""]
    seen = {"system_sha": None, "tools_sha": None}
    printed = set()

    def cut(text) -> str:
        text = "" if text is None else str(text)
        return text if full or len(text) <= _PREVIEW else f"{text[:_PREVIEW]}…（共 {len(text)} 字）"

    def meta(call: dict) -> List[str]:
        """一次模型调用的元信息，外加和上一次比提示词有没有变。"""
        rec, lines = call["record"], []
        if rec is None and not call["expects_record"]:
            return ["    不经模型调用层，没有调用记录"]
        if rec is None:
            return ["    记录缺失：这次调用没有调用记录（记录开关没开，或写入前进程已退出）"]
        lines.append(f"    thinking={'开' if rec['thinking'] else '关'}  输出上限 {rec['max_output_tokens']}  "
                     f"代码版本 {rec['code_version']}  尝试 {rec['idem_key'].rsplit(':', 1)[-1]}  "
                     f"system {_s(rec['system_sha'])}  tools {_s(rec['tools_sha'])}")
        for key, label in (("system_sha", "system prompt"), ("tools_sha", "tools")):
            sha = rec[key]
            if seen[key] is not None and sha != seen[key]:
                lines.append(f"    ！{label} 和上一次调用不同：{_s(seen[key])} → {_s(sha)}")
            seen[key] = sha
            if full and sha in tl["blobs"] and sha not in printed:
                printed.add(sha)
                lines.append(f"    ── {label} {_s(sha)} 全文 ──\n{tl['blobs'][sha]}")
        return lines

    def body(rec: dict, with_request: bool) -> List[str]:
        lines = []
        if with_request and rec["messages"] is not None:
            for m in rec["messages"]:
                lines.append(f"    [{m.get('role')}] {cut(m.get('content') if isinstance(m.get('content'), str) else json.dumps(m.get('content'), ensure_ascii=False))}")
        if rec["error"]:
            lines.append(f"    失败：{json.dumps(rec['error'], ensure_ascii=False)}")
        if rec["reply"]:
            r = rec["reply"]
            if r.get("reasoning"):
                lines.append(f"    思考（{len(r['reasoning'])} 字）：{cut(r['reasoning'])}")
            lines.append(f"    回复[{r['finish_reason']}]：{cut(r['content'])}")
            for c in r["tool_calls"]:
                lines.append(f"    调用 {c['name']} {cut(c['arguments'])}")
        return lines

    for i in tl["items"]:
        at, kind = i["at"].strftime("%H:%M:%S"), i["kind"]
        p = i.get("payload", {})
        tag = f"#{i['seq']}" if "seq" in i else "  "
        if kind == "turn_start":
            out.append(f"── 回合开始 {at} ──")
        elif kind == "turn_end":
            out.append(f"── 回合结束 {at}  {p.get('reason')}  步数 {p.get('steps')}  ¥{p.get('cost_cny')}"
                       f"{'  ' + p['code'] if p.get('code') else ''} ──\n")
        elif kind == "user_message":
            out.append(f"{tag} {at} 用户[{p.get('source', 'user')}]：{cut(p.get('text'))}")
        elif kind == "assistant_message":
            out.append(f"{tag} {at} 模型 {p.get('model')}  in {p.get('tokens_in')} out {p.get('tokens_out')}  "
                       f"¥{p.get('cost_cny')}  {p.get('finish_reason')}")
            call = i["call"]
            if call is None:
                out.append("    账本里找不到对应的调用")
            else:
                out += meta(call)
            full_reasoning = call and call["record"] and call["record"]["reply"] and call["record"]["reply"].get("reasoning")
            shown = full_reasoning or (i["transcript_reasoning"] or {}).get("text")
            if shown:
                out.append(f"    思考（{len(shown)} 字{'' if full_reasoning else '，日志里的截断版'}）：{cut(shown)}")
            if p.get("content"):
                out.append(f"    回复：{cut(p.get('content'))}")
            for c in p.get("tool_calls") or []:
                out.append(f"    → 调用 {c['name']} {cut(c['arguments'])}  [{str(c['id'])[:8]}]")
        elif kind == "tool_result":
            flag = "错误 " if p.get("is_error") else ""
            out.append(f"{tag} {at} ← 结果 [{str(p.get('call_id'))[:8]}] {flag}{cut(p.get('content') if 'content' in p else json.dumps(p, ensure_ascii=False))}")
        elif kind in ("tool_start", "tool_call"):
            continue  # 调用已经在模型回复里列出
        elif kind == "hook":
            if p.get("phase") == "result" and (p.get("decision") != "allow" or p.get("error")):  # 全放行的不显示
                out.append(f"{tag} {at} hook {p.get('hook')}@{p.get('point')} {p.get('decision')} "
                           f"{cut(p.get('reason'))} {cut(p.get('error') or '')}".rstrip())
        elif kind == "model_call":
            c = i["call"]
            state = {"settle": f"¥{c['cost_cny']}  in {c['tokens_in']} out {c['tokens_out']}", "release": "失败，预扣已释放",
                     None: "悬空：预扣没有结算也没有释放"}[c["end"]]
            note = "  ！turn 调用已结算但没有写进日志" if c["purpose"] == "turn" and c["end"] == "settle" else ""
            out.append(f"   {at} 模型调用[{c['purpose']}] {c['model']}  {state}{note}")
            out += meta(c)
            if c["record"]:
                out += body(c["record"], with_request=c["purpose"] != "turn")
        else:
            out.append(f"{tag} {at} {kind} {cut(json.dumps(p, ensure_ascii=False))}")
    t = tl["totals"]
    out += ["", "── 汇总 ──",
            f"回合 {t['turns']}  步数 {t['steps']}  费用 ¥{t['cost_cny']}  token in {t['tokens_in']} out {t['tokens_out']}",
            f"已结算调用 {t['calls']}  失败尝试 {t['failed_attempts']}  悬空预扣 {t['open_holds']}  记录缺失 {t['missing_records']}  错误事件 {t['errors']}",
            f"工具调用 {t['tools']}"]
    return "\n".join(out)


def _s(sha) -> str:
    return sha[:8] if sha else "无"
