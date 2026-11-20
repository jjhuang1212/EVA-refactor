"""用法：python -m devtools.eval <案例目录>... [--runs N] [--tenant 名字] [--label 标签]

开发用：把一个评测案例（case.yaml 加样本）交给真实的 agent loop 跑 N 次，打印每次的结果、费用和断言。
会真调模型，产生费用。每次运行的详情在 devtools/eval/out/<案例>/<run_id>/，排障用 devtools.replay 看会话。
"""
import argparse
import sys

from devtools.eval.case import load
from devtools.eval.runner import run_cases
from eva.contracts.errors import EvaError


def _show(event: dict) -> None:
    if event["event"] == "run_start":
        print(f"[{event['case']} {event['run']}/{event['of']}] 开始 {event['run_id']}  租户 {event['tenant_id']}", flush=True)
        return
    r = event["result"]
    print(f"[{event['case']} {event['run']}/{event['of']}] {'通过' if r['passed'] else '不通过'}  结束原因 {r['reason']}  步数 {r['steps']}  "
          f"费用 {r['cost']['total_cny']} 元  会话 {r['session_id']}")
    for it in r["cost"]["items"]:
        print(f"    {it['purpose']} {it['model']}  {it['count']} 次  {it['cny']} 元")
    for a in r["asserts"]:
        print(f"    {'✓' if a['ok'] else '✗'} {a['name']}（{a['detail']}）")
    if r["error"]:
        print(f"    出错 {r['error']['code']} {r['error']['message']}")
    if len(r["turns"]) > 1:
        print(f"    共 {len(r['turns'])} 轮：" + "；".join(f"{t['reason']} {t['steps']} 步" for t in r["turns"]))
    if r["asked"]:
        print(f"    agent 中途提问 {r['asked']} 次（命令行不交互，提问就会停下；要回答请在网页上选人工或模型扮演用户）")
    if r["files"]:
        print(f"    交付文件：{'、'.join(r['files'])}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m devtools.eval")
    ap.add_argument("case_dirs", nargs="+", help="一个或多个案例目录，按顺序跑，每个各跑 --runs 次")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--label", help="给这组运行起个标签（如 before-v2），之后用 python -m devtools.eval.compare 按标签对比")
    ap.add_argument("--tenant", help="评测租户名，对应的渠道用户 ID 是 eval_<名字>，同名解析到同一个租户；不给则按时间生成。同一租户内相同内容的文件"
                                    "复用转写和解析缓存：要在同一份转写上对比提示词改动，两次用同一个名字；要重新转写，换名字或不给")
    args = ap.parse_args(argv)
    if args.runs < 1:
        ap.error("--runs 必须大于 0")
    try:
        results = run_cases([load(d) for d in args.case_dirs], args.runs, args.tenant, _show, label=args.label)
    except EvaError as e:
        print(f"{e} {e.details or ''}", file=sys.stderr)
        return 1
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
