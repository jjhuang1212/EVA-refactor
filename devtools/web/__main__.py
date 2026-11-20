"""EVA测评系统：python -m devtools.web，默认只听本机（见 devtools/web/config.yaml）。

需要环境变量 EVA_DEVTOOLS_PASSWORD。评测任务会真调模型，产生费用；会话页只读。
可以和终端、企微、模拟器同时开：评测用 eval 类的租户，不用 Redis。
"""
import sys

from dotenv import load_dotenv

from devtools.web import config
from devtools.web.api import make_app
from devtools.web.auth import Auth, password_from_env
from devtools.web.jobs import Jobs
from eva import app as eva_app
from eva.contracts.errors import EvaError


def main() -> int:
    load_dotenv()
    try:
        eva_app.setup_logging("devtools_web")
        cfg = config.load()
        web = make_app(cfg, Auth(password_from_env(), cfg), Jobs(answer_timeout_seconds=cfg.answer_timeout_seconds))
    except EvaError as e:
        print(f"启动失败 {e.code.value}: {e.message} {e.details}", file=sys.stderr)
        return 1
    web.run(cfg.host, cfg.port, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
