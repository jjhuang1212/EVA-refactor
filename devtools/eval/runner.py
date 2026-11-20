"""跑评测案例：和终端渠道走同一个 Conversation（同一个 agent loop），不 mock 模型、不开评测专用旁路。

核心不打印、不读输入，只返回结构化结果；终端和将来的网页都是它的调用方。进度通过回调给出，取消用事件。
每次运行写 out/<案例>/<run_id>/result.json 和交付的文件。会真调模型，会产生费用。
"""
import hashlib
import json
import re
import threading
from decimal import Decimal
from pathlib import Path
from typing import Callable, List, Optional

import yaml
from dotenv import load_dotenv

from devtools.eval import snapshot, usersim
from devtools.eval.case import Case
from eva import sandbox
from eva.app import build, make_audio, make_documents
from eva.common import clock, ids
from eva.common.version import code_version
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import Channel, EvalInteraction, Purpose, TenantRealm, TurnEnd
from eva.model import config as model_config
from eva.model.client import ModelClient
from eva.sandbox import config as sandbox_config
from eva.skills.loader import SkillRegistry
from eva.storage import db
from eva.storage.identities import Identities
from eva.storage.ledger import Ledger, open_ledger
from eva.storage.sessions import Sessions

OUT = Path(__file__).resolve().parent / "out"
CONFIG = Path(__file__).resolve().parent / "config.yaml"
_PREFIX = "eval_"  # 评测的渠道用户 ID 带这个前缀；租户落在 eval realm，调用方指不到真实租户
INPUTS_DIR = "_inputs"  # out/ 下存输入快照的目录，不是案例名
NAME = re.compile(r"[A-Za-z0-9_\-]{1,40}")


def eval_user(name: Optional[str] = None) -> str:
    """评测的渠道用户 ID，同一个名字解析到同一个租户。同一租户内相同内容的文件复用解析和转写缓存：要对比改动就用同一个名字，要重新转写就换名字。
    不给名字则按当前时间生成。"""
    if name is None:
        return _PREFIX + clock.now().strftime("%Y%m%d-%H%M%S")
    if not NAME.fullmatch(name):
        raise EvaError(Code.CONFIG_INVALID, "评测租户名只能含字母、数字、_、-，1 到 40 个字符", name=name)
    return _PREFIX + name


def _tenant_of(user: str) -> str:
    """评测的渠道用户 ID 对应的租户，第一次见到就建。"""
    with db.transaction() as conn:
        return Identities(conn, TenantRealm.EVAL.value).resolve(Channel.CONSOLE.value, "", user)


def check_label(label: Optional[str]) -> None:
    if label is not None and not NAME.fullmatch(label):
        raise EvaError(Code.CONFIG_INVALID, "标签只能含字母、数字、_、-，1 到 40 个字符", label=label)


OVERRIDE_KEYS = ("think_first_steps", "think_after_tools", "max_steps", "system_prompt", "user_sim_prompt")


def check_overrides(overrides: Optional[dict]) -> Optional[dict]:
    """单次覆盖：只对这一批运行生效，不改磁盘上的配置。空表示没有覆盖。字段不对在开跑前就报，不等线程里才发现。"""
    if overrides is None or overrides == {}:
        return None
    bad = lambda why, **kw: EvaError(Code.INVALID_VALUE, f"覆盖项不合法：{why}", **kw)
    if not isinstance(overrides, dict) or set(overrides) - set(OVERRIDE_KEYS):
        raise bad("只能含 " + "、".join(OVERRIDE_KEYS), overrides=repr(overrides)[:200])
    steps, tools, cap, prompt, sim_prompt = (overrides.get(k) for k in OVERRIDE_KEYS)
    if "think_first_steps" in overrides and (isinstance(steps, bool) or not isinstance(steps, int) or steps < 0):
        raise bad("think_first_steps 必须是不小于 0 的整数", value=repr(steps))
    if "max_steps" in overrides and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 1):
        raise bad("max_steps 必须是正整数", value=repr(cap))
    if "think_after_tools" in overrides and (not isinstance(tools, list) or not all(isinstance(t, str) and t for t in tools)):
        raise bad("think_after_tools 必须是工具名列表（可为空）", value=repr(tools))
    if "system_prompt" in overrides and (not isinstance(prompt, str) or not prompt.strip()):
        raise bad("system_prompt 必须是非空文本")
    if "user_sim_prompt" in overrides and (not isinstance(sim_prompt, str) or not sim_prompt.strip()):
        raise bad("user_sim_prompt 必须是非空文本")
    return dict(overrides)


def _inputs(overrides: Optional[dict], files: dict, sim_prompt: Optional[str] = None) -> dict:
    """这一次运行实际用的输入快照：磁盘上的 configs 和 skills（files），加上覆盖项。覆盖也进版本号，对比时才看得出两组不同。
    模型扮演用户时，实际用的模拟用户提示词也进快照（默认的在 config.yaml，不在 configs 下）。
    提示词覆盖直接替换 configs/system_prompt.md 的内容（对比时是行级差异）；思考覆盖单列一个文件。"""
    files = dict(files)
    if overrides:
        if "system_prompt" in overrides:
            files["configs/system_prompt.md"] = overrides["system_prompt"].encode("utf-8")
        think = {k: overrides[k] for k in ("think_first_steps", "think_after_tools", "max_steps") if k in overrides}
        if think:
            files["_overrides/loop.json"] = json.dumps(think, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
    if sim_prompt is not None:
        files["_overrides/user_sim.md"] = sim_prompt.encode("utf-8")
    return files


def _base_shas(files: dict) -> dict:
    """运行开始时磁盘上两个可被覆盖的文件的哈希。「采用」时靠它确认文件在这次运行之后没被别人改过。"""
    return {p: hashlib.sha256(files[p]).hexdigest() for p in ("configs/system_prompt.md", "configs/loop.yaml") if p in files}


def _poll_seconds() -> float:
    try:
        v = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["cost_poll_seconds"]
    except (OSError, yaml.YAMLError, KeyError, TypeError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了评测配置的 cost_poll_seconds", path=str(CONFIG)) from e
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        raise EvaError(Code.CONFIG_INVALID, "cost_poll_seconds 必须是正数", value=repr(v))
    return float(v)


def _spent(tenant_id: str, session_id: str) -> Decimal:
    with open_ledger(tenant_id) as ledger:  # 独立连接，不和正在跑的会话共用事务
        return ledger.settled(session_id, tuple(p.value for p in Purpose))


class _Watch:
    """后台盯一次运行：任务级取消和费用超限都经 run_cancel 让这一次在步骤边界停下（超限只停这一次）。
    读账本失败不静默：记下异常并同样停下这次运行，由调用方判为出错。"""

    def __init__(self, tenant_id: str, session_id: str, limit: Optional[Decimal], job_cancel: threading.Event,
                 run_cancel: threading.Event, poll: float):
        self.spent: Optional[Decimal] = None
        self.error: Optional[Exception] = None
        self._args = (tenant_id, session_id, limit, job_cancel, run_cancel, poll)
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="eval-watch", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        tenant_id, session_id, limit, job_cancel, run_cancel, poll = self._args
        try:
            while not self._done.wait(poll):
                if job_cancel.is_set():
                    run_cancel.set()
                    return
                if limit is not None:
                    spent = _spent(tenant_id, session_id)
                    if spent > limit:
                        self.spent = spent
                        run_cancel.set()
                        return
        except Exception as e:  # 线程里的异常带回调用方处理，不能丢
            self.error = e
            run_cancel.set()

    def stop(self) -> None:
        self._done.set()
        self._thread.join()


def run_cases(cases: List[Case], runs: int = 1, tenant_name: Optional[str] = None,
              on_progress: Optional[Callable[[dict], None]] = None,
              cancel: Optional[threading.Event] = None, out_root: Path = OUT, label: Optional[str] = None,
              poll_seconds: Optional[float] = None, overrides: Optional[dict] = None,
              answer: Optional[Callable[[dict, threading.Event], Optional[str]]] = None, simulate: bool = False) -> List[dict]:
    """按顺序把每个案例各跑 runs 次，每次一个新会话，整次调用共用一个租户。overrides 见 check_overrides。
    cancel 置位后不再开始新的一次（进行中的这次在步骤边界收尾）。label 写进每次的结果，对比时按它选一组运行。
    案例的 max_cost_cny 在运行中就盯着：已结算超限则这一次在步骤边界停下，后面的照常跑。"""
    user = eval_user(tenant_name)
    check_label(label)
    overrides = check_overrides(overrides)
    tenant_id = _tenant_of(user)
    poll = _poll_seconds() if poll_seconds is None else poll_seconds
    code_version.cache_clear()  # 常驻的网页进程里，版本要按这次运行开始时的状态取，不能沿用启动时的
    load_dotenv()
    cancel = cancel or threading.Event()
    cfg = sandbox_config.load()
    documents = make_documents(cfg)
    wiring = (sandbox.create(cfg), cfg, documents, make_audio(documents), SkillRegistry())
    notify = on_progress or (lambda _: None)
    results = []
    for case in cases:
        for n in range(1, runs + 1):
            if cancel.is_set():
                return results
            run_id = ids.new_id()
            notify({"event": "run_start", "case": case.name, "run": n, "of": runs, "run_id": run_id,
                    "tenant_id": tenant_id})
            res = _run_once(case, run_id, tenant_id, wiring, cancel, out_root / case.name / run_id, label,
                            out_root / INPUTS_DIR, poll, overrides, answer, simulate)
            results.append(res)
            notify({"event": "run_done", "case": case.name, "run": n, "of": runs, "result": res})
    return results


def _run_once(case: Case, run_id: str, tenant_id: str, wiring: tuple, cancel: Optional[threading.Event], out: Path,
              label: Optional[str] = None, inputs_store: Optional[Path] = None, poll: float = 1.0,
              overrides: Optional[dict] = None, answer: Optional[Callable[[dict, threading.Event], Optional[str]]] = None,
              simulate: bool = False) -> dict:
    """simulate 为真时由模型扮演用户回答（和 answer 二选一），费用记在单独的会话里，不算进这次运行的 cost。
    answer 在 agent 中途提问时被调用，参数是 {"case","run_id","n","question"} 和这一次的取消事件，返回回答文本；
    返回 None 表示没有回答（等人超时），这次运行判为不通过。不给 answer 就是不交互：agent 一提问，这次运行就此停下。"""
    if simulate and answer is not None:
        raise EvaError(Code.INVALID_VALUE, "模型扮演用户和人工回答不能同时选")
    sim_cfg = usersim.load_config() if simulate else None
    sim_prompt = None if sim_cfg is None else (overrides or {}).get("user_sim_prompt", sim_cfg["user_sim_prompt"])
    out.mkdir(parents=True)
    disk = snapshot.collect()
    res = {"case": case.name, "run_id": run_id, "label": label, "code_version": code_version(),
           "inputs_sha": snapshot.save(_inputs(overrides, disk, sim_prompt), inputs_store or out.parent / "_inputs"), "inputs_changed": None,
           "overrides": overrides, "overrides_base": _base_shas(disk),
           "tenant_id": tenant_id, "started_at": clock.now().isoformat(),
           "session_id": None, "reason": None, "steps": None, "reply": None, "turns": [], "files": [],
           "interaction": (EvalInteraction.MODEL if simulate else EvalInteraction.NONE if answer is None else EvalInteraction.HUMAN).value,
           "asked": 0, "user_sim": None,
           "unanswered_question": None, "stopped_by_cost": None, "error": None}
    run_cancel, watch = threading.Event(), None
    try:
        ov = overrides or {}
        conv = build(tenant_id, "console", *wiring,
                     loop_overrides={k: tuple(v) if k == "think_after_tools" else v for k, v in ov.items() if k not in ("system_prompt", "user_sim_prompt")},
                     prompt_override=ov.get("system_prompt"))
        res["session_id"] = conv.session_id
        watch = _Watch(tenant_id, conv.session_id, case.max_cost_cny, cancel or threading.Event(), run_cancel, poll)
        for f in case.files:
            conv.upload(f.name, f.read_bytes())
        sim = None
        if simulate:  # 模拟用户有自己的会话和账，花费不进 agent 会话的预算，也不触发 max_cost_cny
            with db.transaction() as conn:
                sim_session = Sessions(conn, tenant_id).create(Channel.CONSOLE.value)
            res["user_sim"] = {"session_id": sim_session, "prompt": sim_prompt, "max_rounds": sim_cfg["max_sim_rounds"], "cost_cny": "0"}
            sim = usersim.UserSim(ModelClient(tenant_id, model_config.load()), sim_session, sim_prompt, case.persona, case.message)
        message = case.message
        while True:
            reply = conv.submit(message, run_cancel)
            res["turns"].append({"message": message, "reason": reply.reason, "steps": reply.steps})
            res.update(reason=reply.reason, reply=reply.text, steps=(res["steps"] or 0) + reply.steps)
            for f in reply.files:
                name = Path(f.filename).name
                (out / name).write_bytes(f.data)
                res["files"].append(name)
            if reply.reason != TurnEnd.WAITING_USER.value or run_cancel.is_set():
                break
            res["asked"] += 1
            if sim is not None:  # 超过轮数上限不再替用户回答，这次运行判不通过
                reply_text = sim.reply(reply.text) if res["asked"] <= sim_cfg["max_sim_rounds"] else None
            else:
                reply_text = None if answer is None else answer(
                    {"case": case.name, "run_id": run_id, "n": res["asked"], "question": reply.text}, run_cancel)
            if run_cancel.is_set():  # 等回答的时候被取消或超限停止，不算"没人回答"
                break
            if reply_text is None:  # 没有回答（不交互，或等人超时）：agent 在等用户，不能当成完成
                res["unanswered_question"] = reply.text
                break
            message = reply_text
    except EvaError as e:
        res["error"] = e.to_dict()  # 记下错误码，这次运行判为不通过；其他异常照常向上抛
    finally:
        if watch is not None:
            watch.stop()
    if watch is not None:
        if watch.error is not None:
            if not isinstance(watch.error, EvaError):
                raise watch.error
            res["error"] = res["error"] or watch.error.to_dict()
        res["stopped_by_cost"] = None if watch.spent is None else str(watch.spent)
    res["inputs_changed"] = snapshot.sha_of(_inputs(overrides, snapshot.collect(), sim_prompt)) != res["inputs_sha"]
    res["cost"] = _cost(tenant_id, res["session_id"]) if res["session_id"] else {"total_cny": "0", "items": []}
    if res["user_sim"] is not None:
        res["user_sim"]["cost_cny"] = _cost(tenant_id, res["user_sim"]["session_id"])["total_cny"]
    res["asserts"] = _check(case, res)
    res["passed"] = res["error"] is None and all(a["ok"] for a in res["asserts"])
    (out / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return res


def _cost(tenant_id: str, session_id: str) -> dict:
    """费用按账本已结算的金额汇总，不是估算。"""
    with db.transaction() as conn:
        rows = [r for r in Ledger(conn, tenant_id).session_events(session_id) if r["kind"] == "settle"]
    items = {}
    for r in rows:
        it = items.setdefault((r["purpose"], r["model"]), {"purpose": r["purpose"], "model": r["model"], "count": 0,
                                                           "cny": Decimal(0)})
        it["count"] += 1
        it["cny"] += r["amount_cny"]
    return {"total_cny": str(sum((i["cny"] for i in items.values()), Decimal(0))),
            "items": [{**i, "cny": str(i["cny"])} for i in items.values()]}


def _check(case: Case, res: dict) -> List[dict]:
    """没写的断言不判；运行出错时没有回复和步数，断言按不通过记录，不跳过。"""
    out = []

    def add(name: str, ok: bool, detail: str) -> None:
        out.append({"name": name, "ok": ok, "detail": detail})

    if res["inputs_changed"]:  # 输入没变是常态，不占一条断言
        add("inputs_unchanged_during_run", False, "运行期间提示词、skill、配置被改动，这次结果不可比")
    if res["unanswered_question"] is not None:
        why = {EvalInteraction.NONE.value: "这次评测没有选人工回答", EvalInteraction.HUMAN.value: "等人回答超时",
               EvalInteraction.MODEL.value: "模拟用户回答到上限了，agent 仍在提问"}[res["interaction"]]
        add("answered_all_questions", False, f"agent 提问停下，{why}：{res['unanswered_question'][:80]}")
    if case.max_questions is not None:
        add("max_questions", res["asked"] <= case.max_questions, f"提问 {res['asked']} 次，上限 {case.max_questions}")

    for w in case.reply_contains:
        add(f"reply_contains:{w}", res["reply"] is not None and w in res["reply"], "最终回复" + ("包含" if res["reply"] and w in res["reply"] else "不包含"))
    for w in case.reply_not_contains:  # 没有回复就确认不了"没出现"，按不通过记
        has_reply = res["reply"] is not None
        add(f"reply_not_contains:{w}", has_reply and w not in res["reply"],
            "没有最终回复，确认不了" if not has_reply else "最终回复" + ("不包含" if w not in res["reply"] else "包含了"))
    if case.max_steps is not None:
        add("max_steps", res["steps"] is not None and res["steps"] <= case.max_steps, f"{res['steps']} 步，上限 {case.max_steps}")
    if case.max_cost_cny is not None:
        total = Decimal(res["cost"]["total_cny"])
        if res["stopped_by_cost"] is not None:  # 在步骤边界才能停，最终费用可能比停下时看到的还高
            add("max_cost_cny", False, f"中途停止：已结算 {res['stopped_by_cost']} 元超过上限 {case.max_cost_cny}，最终 {total} 元")
        else:
            add("max_cost_cny", total <= case.max_cost_cny, f"{total} 元，上限 {case.max_cost_cny}")
    return out
