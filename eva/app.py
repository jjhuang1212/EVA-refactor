"""装配层：把各层接起来，交给渠道一个"提交消息、拿到回复"的回调。

可以 import 所有层，没有任何层 import 它。渠道只拿到 Conversation.submit，碰不到存储、模型和工具。
"""
import logging
import mimetypes
import os
import socket
import sys
import threading
from decimal import Decimal
from pathlib import Path
from dataclasses import replace
from typing import Callable, Optional, Sequence, Union

import yaml
from dotenv import load_dotenv

from eva.common import ids, logs
from eva.contracts import citation
from eva.contracts.channel import ChannelMessage, Reply
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import Channel, EventType, Purpose, TenantRealm, TurnEnd
from eva.hooks import config as hooks_config
from eva.hooks.base import Hook
from eva.hooks.citations import CitationHook
from eva.hooks.delivery import DeliveryGateHook
from eva.hooks.user_only import UserOnlyHook
from eva.loop import compaction, config as loop_config, restore as restore_info
from eva.loop.config import LoopConfig
from eva.loop.turn import run_turn
from eva.model import asr as asr_module
from eva.model import filetrans
from eva.model import config as model_config
from eva.model.client import ModelClient
from eva import sandbox
from eva.sandbox import config as sandbox_config
from eva.session import config as session_config
from eva.session.runner import SessionRunner
from eva.session.sweeper import Sweeper
from eva.storage import db, tenants
from eva.storage.channel_messages import ChannelMessages
from eva.storage.files import Files
from eva.storage.identities import Identities
from eva.storage.ledger import Ledger
from eva.storage.objects import ObjectStore, Objects
from eva.storage.sessions import Sessions
from eva.storage.tenant_memories import TenantMemories
from eva.storage.tenant_skills import SkillLimits, TenantSkills
from eva.storage.transcript import Transcript
from eva.tools.ask_user import ask_user
from eva.tools.audio import Audio, load_transcript
from eva.tools.base import ToolRegistry
from eva.skills.loader import SkillRegistry, Skills, SkillView
from eva.skills.loader import render as render_skill
from eva.tools.deliver_file import Outbox, make_deliver_file
from eva.tools.notify_user import make_notify_user
from eva.tools import load_skill as load_skill_tool
from eva.tools.load_skill import make_load_skill
from eva.tools import config as tools_config
from eva.tools.documents import Documents
from eva.tools.examples import get_time
from eva.tools.proofread import make_proofread
from eva.tools.list_files import make_list_files
from eva.tools.read_file import make_read_file
from eva.tools.confirm_term import make_confirm_term
from eva.tools.memory import make_forget, make_remember
from eva.tools.new_topic import NEW_TOPIC_REPLY, make_new_topic
from eva.tools.export_transcript import make_export_transcript
from eva.tools.run_python import make_run_python
from eva.tools.search_hotwords import make_search_hotwords
from eva.tools.confirm_alias import make_confirm_alias
from eva.tools.qcc import Qcc
from eva.tools.query_company import make_query_company
from eva.tools.resolve_entity import make_resolve_entity
from eva.tools.web_search import make_web_search
from eva.tools.view_image import make_view_image
from eva.tools.vision import Vision
from eva.tools.write_file import make_write_file

SYSTEM_PROMPT_PATH = Path(__file__).resolve().parents[1] / "configs" / "system_prompt.md"
LOGGING_PATH = Path(__file__).resolve().parents[1] / "configs" / "logging.yaml"
_LOG_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}
CONSOLE_USER = "dev_local"  # 本机终端只有一个使用者，渠道用户 ID 固定
VOICE_PREFIX = "[语音转文字，可能有错字] "
GATE_FAILED_NOTE = "\n\n（注：上面回答里的部分引用没有通过原文核对，数字和说法请以原文为准。）"
NO_SPEECH_REPLY = "没有听清这段语音里说了什么，请再说一遍，或者直接发文字。"
log = logging.getLogger("eva.app")


class Conversation:
    """一个租户的对话。每次 submit 跑一个 turn，状态全在日志里。

    resume 为 False 时新开一个会话（终端）；为 True 时接着本租户本渠道最近的会话，没有才新开（企微）。
    会话永续，不按空闲时间轮换；想从头聊用 context_reset 事件，不新建会话。"""

    def __init__(self, tenant_id: str, channel: str, model: ModelClient, tools: ToolRegistry,
                 hooks: Sequence[Hook], loop_cfg: LoopConfig, system_prompt: Union[str, Callable[[], str]], store: ObjectStore,
                 open_tx: Callable = db.transaction, resume: bool = False,
                 outbox: Optional[Outbox] = None, audio: Optional[Audio] = None,
                 restore: Optional[compaction.Restore] = None):
        self.tenant_id, self._model, self._tools, self._hooks, self._store = tenant_id, model, tools, hooks, store
        self._audio, self._restore = audio, restore
        self._outbox = outbox or Outbox()  # 和 deliver_file 工具共用同一个，装配时传入
        # 传函数则每个 turn 开头取一次（skill 清单会热加载），传字符串则固定不变
        self._system_prompt = system_prompt if callable(system_prompt) else (lambda: system_prompt)
        self._cfg, self._open_tx = loop_cfg, open_tx
        with open_tx() as conn:
            if tenants.get(conn, tenant_id) is None:
                raise EvaError(Code.TENANT_NOT_FOUND, "租户不存在，先经 Identities.resolve 取得", tenant_id=tenant_id)
            sessions = Sessions(conn, tenant_id)
            last = sessions.latest(channel) if resume else None
            self.session_id = last["session_id"] if last else sessions.create(channel)

    def _spill(self, session_id: str, text: str) -> str:
        with self._open_tx() as conn:
            return Objects(conn, self.tenant_id, self._store).put_overflow(session_id, text.encode("utf-8"))

    def upload(self, filename: str, data: bytes, msg_id: Optional[str] = None) -> dict:
        """登记并存下用户上传的文件，再在日志里留一条语义事件，让模型知道有这个文件和它的 file_id。
        msg_id 是渠道消息 ID，已处理过的重复投递返回 duplicate=True（文件登记和存储本身幂等，不会重复）。"""
        if not filename:
            raise EvaError(Code.INVALID_VALUE, "文件名不能为空")
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with self._open_tx() as conn:
            row, _ = Objects(conn, self.tenant_id, self._store).save_original(filename, mime, data)
        filename = row["filename"]  # 同名不同内容时登记会改名，模型和用户看到的、引用用的都是改后的名字
        note = f"[用户上传了文件] 文件名：{filename}，file_id：{row['file_id']}，大小：{len(data)} 字节"
        with SessionRunner(self.tenant_id, self.session_id, self._open_tx).writing() as w:
            written = w.append_user_message(msg_id or ids.new_id(), note)
        return {"file_id": row["file_id"], "filename": filename, "bytes": len(data), "duplicate": written is None}

    def voice(self, data: bytes, ext: str, cancel: threading.Event, msg_id: str, on_file=None) -> Optional[Reply]:
        """语音消息：转写成文字，当作用户的一条文字消息交给 submit。原始语音按文件存下（证据，也让重复投递命中转写缓存）。
        没有识别出说话内容时不进模型，直接回一句提示。msg_id 已处理过的重复投递按 submit 的规则办。"""
        if self._audio is None:
            raise EvaError(Code.CONFIG_INVALID, "未装配语音转写")
        with self._open_tx() as conn:
            if Transcript(conn, self.tenant_id).find_user_message(self.session_id, msg_id) is not None:
                return self.submit("", cancel, msg_id, on_file)  # 已记过，文字不会被用到
        name = f"语音_{msg_id[:8]}{ext}"  # 同一条消息重投递得到同名同内容，登记幂等
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with self._open_tx() as conn:
            row, _ = Objects(conn, self.tenant_id, self._store).save_original(name, mime, data)
        _, parsed = load_transcript(self._audio, self._store, self._open_tx, self.tenant_id, self.session_id,
                                    row["file_id"], cancel)
        if not parsed.blocks:
            return Reply(NO_SPEECH_REPLY, "no_speech", 0, Decimal(0), [])
        return self.submit(VOICE_PREFIX + "".join(b.text for b in parsed.blocks), cancel, msg_id, on_file)

    def submit(self, text: str, cancel: threading.Event, msg_id: Optional[str] = None, on_file=None) -> Optional[Reply]:
        """写入用户消息并跑一个 turn。模型层、账本、存储的错误原样抛给渠道展示。

        on_file 是渠道的即时投递回调：deliver_file 带 immediately 的文件当场交给它，不等 turn 结束。
        msg_id 是渠道消息 ID。重复投递时：上次的 turn 已正常结束就返回 None；
        上次中途失败或崩溃（没有 turn_end，或以 error 结束）就接着重跑，不重复写用户消息。"""
        with SessionRunner(self.tenant_id, self.session_id, self._open_tx).writing() as w:
            msg_id = msg_id or ids.new_id()
            first = w.append_input(msg_id, text, self._tools.parking_names())
            if first is None:
                first = self._unfinished_from(w, msg_id)
                if first is None:
                    return None
            self._outbox.clear()  # 上一轮失败留下的登记不能带进这一轮
            self._outbox.set_sink(on_file)
            before = self._tool_spend()
            try:
                out = run_turn(w, self._model, self._tools, self._hooks, self._cfg, self._system_prompt(), cancel,
                               self._spill, self._restore)
            finally:
                self._outbox.set_sink(None)
            cost = out.cost_cny + self._tool_spend() - before
            text = out.text + GATE_FAILED_NOTE if out.reason is TurnEnd.GATE_FAILED and out.text else out.text
            return Reply(citation.readable(text), out.reason.value, out.steps, cost, w.events(first), self._outbox.take(), out.options)

    def reset_topic(self) -> str:
        """用户明确要求换话题：此后的模型视图从这里重新开始，日志原文保留。返回给用户的回复。"""
        with SessionRunner(self.tenant_id, self.session_id, self._open_tx).writing() as w:
            w.append(EventType.CONTEXT_RESET.value, {"reason": "new_topic_command"})
        return NEW_TOPIC_REPLY

    def _tool_spend(self) -> Decimal:
        """本会话工具和转写已结算的花费。循环里的 cost_cny 只含模型调用，企业数据查询这类要另算。"""
        with self._open_tx() as conn:
            return Ledger(conn, self.tenant_id).settled(self.session_id, (Purpose.TOOL_CALL.value, Purpose.ASR.value))

    def _unfinished_from(self, w, msg_id: str) -> Optional[int]:
        """该条输入之后还没有正常收尾的 turn：返回输入的序号，供重跑；已正常收尾返回 None。"""
        with self._open_tx() as conn:
            seq = Transcript(conn, self.tenant_id).find_user_message(self.session_id, msg_id)
        ended = [e["payload"].get("reason") for e in w.events(seq) if e["event_type"] == EventType.TURN_END.value]
        return seq if all(r == TurnEnd.ERROR.value for r in ended) else None


class Hub:
    """多租户入口：每个租户一个 Conversation，模型调用按租户记账。渠道拿到的是它，碰不到别的。"""

    def __init__(self, make: Callable[[str], Conversation]):
        self._make, self._convs, self._lock = make, {}, threading.Lock()

    def _conv(self, tenant_id: str) -> Conversation:
        with self._lock:
            if tenant_id not in self._convs:
                self._convs[tenant_id] = self._make(tenant_id)
            return self._convs[tenant_id]

    def submit(self, tenant_id: str, text: str, msg_id: str, cancel: threading.Event, on_file=None) -> Optional[Reply]:
        return self._conv(tenant_id).submit(text, cancel, msg_id, on_file)

    def upload(self, tenant_id: str, filename: str, data: bytes, msg_id: str) -> Optional[dict]:
        info = self._conv(tenant_id).upload(filename, data, msg_id)
        return None if info["duplicate"] else info

    def voice(self, tenant_id: str, data: bytes, ext: str, msg_id: str, cancel: threading.Event, on_file=None) -> Optional[Reply]:
        return self._conv(tenant_id).voice(data, ext, cancel, msg_id, on_file)

    def reset_topic(self, tenant_id: str) -> str:
        return self._conv(tenant_id).reset_topic()


def record_message(tenant_id: str, message: ChannelMessage) -> None:
    """渠道适配器记一条收发的消息；只用于重建对话和排障。"""
    with db.transaction() as conn:
        ChannelMessages(conn, tenant_id).add(message)


def make_resolver(realm: str, channel: str) -> Callable[[str, str], str]:
    """(范围, 渠道用户 ID) -> 租户编号，结果在进程内缓存。第一版身份不会改指向；做账号合并时这里要能失效。"""
    cache, lock = {}, threading.Lock()

    def resolve(scope: str, external_id: str) -> str:
        with lock:
            if (scope, external_id) in cache:
                return cache[(scope, external_id)]
        with db.transaction() as conn:
            tenant_id = Identities(conn, realm).resolve(channel, scope, external_id)
        with lock:
            cache[(scope, external_id)] = tenant_id
        return tenant_id

    return resolve


def make_documents(sandbox_cfg: sandbox_config.SandboxConfig) -> Documents:
    """文件解析用独立的沙箱实例（自己的镜像、并发上限），全进程共用一个。"""
    parsing_cfg = tools_config.load_parsing(sandbox_cfg)
    return Documents(sandbox.create(parsing_cfg.sandbox), parsing_cfg, Vision(model_config.load().vision, parsing_cfg.vision))


def make_audio(documents: Documents) -> Audio:
    """录音转写：预处理用解析沙箱，转写按租户记账。启动时就核对密钥和地址，不等到第一条语音。"""
    cfg = asr_module.load()
    asr_module.credentials(cfg)
    budget = model_config.load().budget_per_day_cny
    return Audio(documents, lambda tenant_id: asr_module.Asr(tenant_id, cfg, budget), cfg,
                 lambda tenant_id: filetrans.Filetrans(tenant_id, cfg, budget))


def _web_search():
    load_dotenv()
    env = {n: os.environ.get(n) for n in ("ZHIPU_API_KEY", "ZHIPU_BASE_URL")}
    missing = [n for n, v in env.items() if not v]
    if missing:
        raise EvaError(Code.CONFIG_INVALID, "缺少联网搜索的环境变量", missing=missing)
    return make_web_search(tools_config.load_web_search(), env["ZHIPU_BASE_URL"], env["ZHIPU_API_KEY"])


def _qcc_tools() -> list:
    load_dotenv()
    key = os.environ.get("QCC_API_KEY")
    if not key:
        raise EvaError(Code.CONFIG_INVALID, "缺少企业数据源的环境变量", missing=["QCC_API_KEY"])
    cfg = tools_config.load_qcc()
    qcc = Qcc(cfg, key)
    return [make_resolve_entity(cfg, qcc), make_query_company(cfg, qcc), make_confirm_alias()]


def skill_limits(registry: SkillRegistry) -> SkillLimits:
    cfg = registry.config
    return SkillLimits(frozenset(registry.current().names()), cfg.max_body_bytes, cfg.max_description_chars, cfg.max_tenant_skills)


def memory_note(memories: list) -> str:
    """用户让记住的偏好和纠正，编号给 forget 用。没有就不附，提示词和没有这个功能时一样。"""
    if not memories:
        return ""
    return "\n用户让你记住的偏好和纠正（编号用于 forget）：\n" + "\n".join(f"- #{m['seq']} {m['text']}" for m in memories) + "\n"


def system_prompt(skills: Union[Skills, SkillView], text: Optional[str] = None) -> str:
    """text 给了就用它代替 configs/system_prompt.md（评测的单次覆盖用），skill 清单照常附上。"""
    text = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8") if text is None else text
    if not skills:
        return text
    return f"{text.rstrip()}\n可用的 skill（判断某类事情该怎么做时，先用 load_skill 读正文，再照它做）：\n{skills.catalog()}\n"


def _call_recorder(tenant_id: str, store: ObjectStore):
    def record(session_id: str, name: str, rec: dict, blobs: dict) -> None:
        with db.transaction() as conn:
            Objects(conn, tenant_id, store).put_call_record(session_id, name, rec, blobs)
    return record


def build(tenant_id: str, channel: str, box: sandbox.Sandbox, sandbox_cfg: sandbox_config.SandboxConfig,
          documents: Documents, audio: Audio, skill_registry: SkillRegistry,
          resume: bool = False, loop_overrides: Optional[dict] = None,
          prompt_override: Optional[str] = None) -> Conversation:
    """skill_registry 全进程共用一个：文件指纹的检查只做一次，与租户数无关。
    loop_overrides 和 prompt_override 只对这一次装配生效，不改磁盘上的配置（评测用）；字段名不对时 replace 会抛错。"""
    store, model_cfg = ObjectStore.from_config(), model_config.load()
    model = ModelClient(tenant_id, model_cfg, recorder=_call_recorder(tenant_id, store) if model_cfg.call_record else None)
    loop_cfg, read_cfg = replace(loop_config.load(), **(loop_overrides or {})), tools_config.load_read_file()
    if read_cfg.page_bytes >= loop_cfg.spill_threshold_bytes:  # 读取工具自己不溢出，一页必须放得进阈值
        raise EvaError(Code.CONFIG_INVALID, "read_file.page_bytes 必须小于 spill_threshold_bytes",
                       page_bytes=read_cfg.page_bytes, spill_threshold_bytes=loop_cfg.spill_threshold_bytes)
    if documents.cfg.limits.block_max_chars * 4 >= read_cfg.page_bytes:  # 最坏按每字符 4 字节，一个块必须放得进一页
        raise EvaError(Code.CONFIG_INVALID, "解析的 block_max_chars 乘 4 必须小于 read_file.page_bytes",
                       block_max_chars=documents.cfg.limits.block_max_chars, page_bytes=read_cfg.page_bytes)
    outbox, skills = Outbox(), SkillView(skill_registry, TenantSkills(tenant_id, lambda: skill_limits(skill_registry)))
    tools = ToolRegistry([get_time, ask_user, make_read_file(store, read_cfg, documents=documents, audio=audio, model=model),
                          make_run_python(box, store, sandbox_cfg, skill_files=skills.script_files),
                          make_write_file(box, store), make_deliver_file(store, outbox, tools_config.load_deliver_file()),
                          make_list_files(tools_config.load_list_files()),
                          make_notify_user(outbox),
                          make_search_hotwords(tools_config.load_search_hotwords()), make_export_transcript(store, audio),
                          make_confirm_term(tools_config.load_confirm_term()),
                          make_remember(tools_config.load_memory()), make_forget(), make_new_topic(),
                          make_view_image(store, tools_config.load_view_image(), model, documents),
                          make_proofread(store, tools_config.load_proofread(), model),
                          *_qcc_tools(),
                          _web_search(), make_load_skill(skills)])  # 启动时没有 skill 也注册，热加载后新增的才读得到
    loop_config.check_tools(loop_cfg, tools.names())
    loop_config.check_window(loop_cfg, model.context_window)
    user_only = hooks_config.load_user_only()
    hooks_config.check_user_only(user_only, tools.names())
    hooks = [CitationHook(store, hooks_config.load_citation(), read_cfg.max_file_bytes,
                          max_deliver_bytes=tools_config.load_deliver_file().max_file_bytes,
                          scenarios=hooks_config.load_scenarios()),
             DeliveryGateHook(hooks_config.load_delivery_gates()), UserOnlyHook(user_only)]

    def turn_prompt() -> str:
        skills.refresh()
        with db.transaction() as conn:
            memories = TenantMemories(conn, tenant_id).active()
        return system_prompt(skills, prompt_override) + memory_note(memories)

    def recent_files(limit: int) -> list:
        with db.transaction() as conn:
            return Files(conn, tenant_id).recent(limit)

    def restore(events: list, cut_seq: int) -> dict:
        return restore_info.build(events, cut_seq, loop_cfg, load_skill_tool.NAME, lambda name: render_skill(skills.get(name)),
                                  recent_files)

    return Conversation(tenant_id, channel, model, tools, hooks, loop_cfg,
                        turn_prompt, store, resume=resume,
                        outbox=outbox, audio=audio, restore=restore)


def load_logging(path: Path = LOGGING_PATH) -> logs.LogConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了日志配置", path=str(path), error=str(e)) from e
    if not isinstance(raw, dict):
        raise EvaError(Code.CONFIG_INVALID, "日志配置顶层必须是映射", path=str(path))
    levels = {}
    for key in ("console_level", "file_level"):
        if raw.get(key) not in _LOG_LEVELS:
            raise EvaError(Code.CONFIG_INVALID, f"{key} 必须是 {'/'.join(_LOG_LEVELS)} 之一", value=repr(raw.get(key)))
        levels[key] = _LOG_LEVELS[raw[key]]
    ints = {}
    for key, minimum in (("keep_days", 1), ("preview_chars", 0)):
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
            raise EvaError(Code.CONFIG_INVALID, f"{key} 必须是不小于 {minimum} 的整数", value=repr(v))
        ints[key] = v
    if not isinstance(raw.get("color"), bool):
        raise EvaError(Code.CONFIG_INVALID, "color 必须是布尔值", value=repr(raw.get("color")))
    directory = raw.get("directory")
    if not isinstance(directory, str) or not directory.strip():
        raise EvaError(Code.CONFIG_INVALID, "directory 必须是非空字符串", value=repr(directory))
    return logs.LogConfig(directory=Path(__file__).resolve().parents[1] / directory, color=raw["color"], **levels, **ints)


def setup_logging(name: str, terminal: bool = True, path: Path = LOGGING_PATH) -> None:
    """各入口启动时先调。终端交互的 console 渠道传 terminal=False，日志只进文件，不和对话界面混在一起。"""
    cfg = load_logging(path)
    try:
        logs.setup(cfg, name, terminal)
    except OSError as e:
        raise EvaError(Code.CONFIG_INVALID, "日志目录或文件不可写", directory=str(cfg.directory), error=str(e)) from e


def _report(error: EvaError, context: dict) -> None:
    logs.report_error(logging.getLogger("eva.channels.wecom"), error, **context)


def _wecom_env() -> dict:
    load_dotenv()
    names = ("WECOM_CORP_ID", "WECOM_KF_SECRET", "WECOM_CALLBACK_TOKEN", "WECOM_ENCODING_AES_KEY")
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise EvaError(Code.CONFIG_INVALID, "缺少企微环境变量", missing=missing)
    return {n: os.environ[n] for n in names}


def make_web(callback):
    """把企微回调挂到 HTTP 路由上。路由里只转发，验签、解密、入队都在 Callback 里。"""
    from flask import Flask, request

    web = Flask(__name__)

    @web.get("/wechat/callback")
    def verify():
        status, body = callback.verify(request.args)
        return body, status

    @web.post("/wechat/callback")
    def receive():
        status, body = callback.receive(request.args, request.get_data())
        return body, status

    return web


def serve_wecom() -> int:
    """回调服务在后台线程，消息处理在主线程：处理线程出了未预期的异常，整个进程带着堆栈退出，不留一个只收不处理的空壳。"""
    import requests

    from eva.channels.wecom import config as wecom_config
    from eva.channels.wecom.callback import Callback
    from eva.channels.wecom.client import WecomClient
    from eva.channels.wecom.worker import Worker
    from eva.storage.redis_state import RedisState, connect_from_env

    cfg, env = wecom_config.load(), _wecom_env()
    state = RedisState(connect_from_env(), cfg.key_prefix)
    sbx_cfg = sandbox_config.load()
    box = sandbox.create(sbx_cfg)  # 全进程共用一个，并发上限才是全局的
    documents = make_documents(sbx_cfg)
    audio = make_audio(documents)
    skill_registry = SkillRegistry()
    hub = Hub(lambda tenant_id: build(tenant_id, "wecom", box, sbx_cfg, documents, audio, skill_registry, resume=True))
    client = WecomClient(cfg, requests.Session(), env["WECOM_CORP_ID"], env["WECOM_KF_SECRET"])
    callback = Callback(env["WECOM_CALLBACK_TOKEN"], env["WECOM_ENCODING_AES_KEY"], state.push)
    stop = threading.Event()
    worker = Worker(cfg, client, state, hub, _report, stop, resolve=make_resolver(TenantRealm.USR.value, Channel.WECOM.value),
                    record=record_message)

    web = make_web(callback)
    threading.Thread(target=lambda: web.run(cfg.listen_host, cfg.listen_port), daemon=True).start()
    sweeper = Sweeper(session_config.load())
    sweeper.start()
    log.info("企微服务已启动 监听 %s:%s 沙箱后端 %s", cfg.listen_host, cfg.listen_port, sbx_cfg.backend)
    try:
        worker.run_forever(f"{socket.gethostname()}-{os.getpid()}")
    except KeyboardInterrupt:
        stop.set()
    finally:
        sweeper.stop()
    return 0


def main(argv: Sequence[str]) -> int:
    if list(argv) not in (["console"], ["wecom"]):
        print("用法: python -m eva.app console | wecom", file=sys.stderr)
        return 2
    try:
        setup_logging(argv[0], terminal=list(argv) == ["wecom"])
        if list(argv) == ["wecom"]:
            return serve_wecom()
        from eva.channels import console  # 渠道只在这里接入

        sweeper = Sweeper(session_config.load())
        sweeper.start()
        try:
            sbx_cfg = sandbox_config.load()
            documents = make_documents(sbx_cfg)
            tenant_id = make_resolver(TenantRealm.DEV.value, Channel.CONSOLE.value)("", CONSOLE_USER)
            conv = build(tenant_id, "console", sandbox.create(sbx_cfg), sbx_cfg, documents, make_audio(documents),
                         SkillRegistry())
            return console.run(conv.submit, conv.upload)
        finally:
            sweeper.stop()
    except EvaError as e:
        print(f"启动失败 {e.code.value}: {e.message} {e.details}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

