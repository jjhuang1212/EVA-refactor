"""本机企微客服模拟器：python -m tools.wecom_sim，浏览器打开 http://127.0.0.1:5001

真实的：企微 Worker、Redis 事件队列与游标、核心、模型（会产生费用）、SQL Server、MinIO、工具与出处校验。
模拟的：企微的 sync_msg、素材下载、发消息。不验签，不走回调，不覆盖视频和客服事件。
页面上显示成功不等于企微验收通过。
"""
import dataclasses
import logging
import os
import sys
import threading

from dotenv import load_dotenv

from eva import app as eva_app
from eva import sandbox
from eva.channels.wecom import config as wecom_config
from eva.channels.wecom.worker import Worker
from eva.contracts.vocab import Channel, TenantRealm
from eva.sandbox import config as sandbox_config
from eva.skills.loader import SkillRegistry
from eva.storage.redis_state import RedisState, connect_from_env
from tools.wecom_sim.server import make_app
from tools.wecom_sim.sim import Mailbox, SimClient, TracingHub, report_to

HOST, PORT = "127.0.0.1", 5001
DEFAULT_KF = "sim_kf_001"
log = logging.getLogger("tools.wecom_sim")
PREFIX = "eva_sim"  # 和正式的队列、游标分开，模拟器的事件不会被真实 worker 取走


def main() -> int:
    load_dotenv()
    eva_app.setup_logging("wecom_sim")
    cfg = dataclasses.replace(wecom_config.load(), key_prefix=PREFIX)
    sbx_cfg = sandbox_config.load()
    box_sandbox = sandbox.create(sbx_cfg)
    documents = eva_app.make_documents(sbx_cfg)
    audio = eva_app.make_audio(documents)
    skill_registry = SkillRegistry()
    hub = eva_app.Hub(lambda tenant: eva_app.build(tenant, "wecom", box_sandbox, sbx_cfg, documents, audio,
                                                   skill_registry, resume=True))
    state = RedisState(connect_from_env(), cfg.key_prefix)
    resolve = eva_app.make_resolver(TenantRealm.SIM.value, Channel.WECOM.value)
    mailbox = Mailbox(cfg, lambda kf: state.push("sim-token", kf), resolve)
    worker = Worker(cfg, SimClient(mailbox), state, TracingHub(hub, mailbox), report_to(mailbox), resolve=resolve,
                    record=eva_app.record_message)
    web = make_app(mailbox, cfg, DEFAULT_KF)

    def run_worker():
        try:
            worker.run_forever(f"sim-{os.getpid()}")
        except BaseException:  # 处理线程挂了就整个退出，不留一个能发消息却没人处理的页面
            log.critical("处理线程异常退出，模拟器随之退出", exc_info=True)
            os._exit(1)

    threading.Thread(target=run_worker, daemon=True).start()
    log.info("模拟器已启动：http://%s:%s  沙箱后端：%s", HOST, PORT, sbx_cfg.backend)
    web.run(HOST, PORT, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

