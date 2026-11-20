"""用法：python -m devtools.replay <tenant_id> <session_id> [--full]

开发排障用：把一个会话的每一轮、每次模型调用的请求和回复、思考、工具往返、费用打印出来。只读。
"""
import argparse
import sys

from devtools.replay.render import render
from devtools.replay.timeline import build
from eva.contracts.errors import EvaError
from eva.storage import db
from eva.storage.objects import ObjectStore


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m devtools.replay")
    ap.add_argument("tenant_id")
    ap.add_argument("session_id")
    ap.add_argument("--full", action="store_true", help="正文不截断，并打印 system prompt 和 tools 全文")
    args = ap.parse_args(argv)
    try:
        with db.transaction() as conn:
            tl = build(conn, ObjectStore.from_config(), args.tenant_id, args.session_id, with_blobs=args.full)
    except EvaError as e:
        print(f"{e.code.value} {e}", file=sys.stderr)
        return 1
    print(render(tl, full=args.full))
    return 0


if __name__ == "__main__":
    sys.exit(main())
