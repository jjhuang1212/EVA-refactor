"""评测任务的后台运行器：同一时刻最多一个，每次花钱的运行都由人明确启动。

进度来自 run_case 的回调，取消用它的事件。状态只在内存里：进程重启后没有"当前任务"，
已写出 result.json 的运行照常能在结果列表里看到。
"""
import copy
import logging
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

from devtools.eval.case import Case
from devtools.eval.runner import OUT, check_label, check_overrides, eval_user, run_cases
from eva.common import clock, ids
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import EvalInteraction, check

log = logging.getLogger(__name__)


class Jobs:
    def __init__(self, run: Callable = run_cases, out_root: Path = OUT, *, answer_timeout_seconds: float):
        self._run, self._out_root, self._answer_timeout = run, out_root, answer_timeout_seconds
        self._answered, self._reply = threading.Event(), None  # 人工回答：回答到了就置位，正文放 _reply
        self._lock = threading.Lock()
        self._job: Optional[dict] = None
        self._cancel: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, cases: List[Case], runs: int, tenant_name: Optional[str], label: Optional[str],
              overrides: Optional[dict] = None, interaction: str = EvalInteraction.NONE.value) -> dict:
        """按顺序把每个案例各跑 runs 次。overrides 是只对这一批生效的覆盖项（思考、系统提示词）。
        interaction 决定 agent 中途提问时怎么办：none 这次运行就此停下，human 等人在网页上回答（等待期间任务一直占着这唯一的评测位）。"""
        if isinstance(runs, bool) or not isinstance(runs, int) or runs < 1:
            raise EvaError(Code.INVALID_VALUE, "runs 必须是正整数", runs=runs)
        names = [c.name for c in cases]
        if not names or len(set(names)) != len(names):
            raise EvaError(Code.INVALID_VALUE, "cases 必须是非空且不重复的案例列表", cases=names)
        eval_user(tenant_name)  # 名字不合法在这里就报，不等线程里才发现
        check_label(label)
        overrides = check_overrides(overrides)
        interaction = check(EvalInteraction, interaction).value
        with self._lock:
            if self._job is not None and self._job["state"] == "running":
                raise EvaError(Code.EVAL_BUSY, "已有评测任务在跑", job_id=self._job["job_id"], cases=self._job["cases"])
            self._cancel = threading.Event()
            self._job = {"job_id": ids.new_id(), "cases": names, "runs": runs, "tenant_name": tenant_name,
                         "label": label, "overrides": overrides, "interaction": interaction, "question": None, "state": "running", "cancelling": False,
                         "started_at": clock.now().isoformat(), "items": [], "error": None}
            self._thread = threading.Thread(target=self._work, args=(cases, runs, tenant_name, label, overrides, interaction, self._cancel),
                                            name="eval-job", daemon=True)
            self._thread.start()
            return copy.deepcopy(self._job)

    def _progress(self, event: dict) -> None:
        with self._lock:
            if event["event"] == "run_start":
                self._job["items"].append({"case": event["case"], "run": event["run"], "run_id": event["run_id"],
                                           "tenant_id": event["tenant_id"], "done": False})
            else:
                r = event["result"]
                self._job["items"][-1].update(done=True, passed=r["passed"], reason=r["reason"], steps=r["steps"],
                                              cost_cny=r["cost"]["total_cny"], session_id=r["session_id"],
                                              error_code=r["error"]["code"] if r["error"] else None)

    def _human(self, info: dict, run_cancel: threading.Event) -> Optional[str]:
        """agent 提问时调用：把问题挂在任务上，等页面提交回答。取消或超时返回 None（超时由调用方判这次不通过）。"""
        with self._lock:
            self._job["question"] = {**info, "asked_at": clock.now().isoformat()}
            self._reply = None
            self._answered.clear()
        deadline = time.monotonic() + self._answer_timeout
        try:
            while not self._answered.wait(0.5):
                if self._cancel.is_set():
                    run_cancel.set()  # 任务取消不等监视线程转达，免得被当成"没人回答"
                if run_cancel.is_set() or time.monotonic() >= deadline:
                    break
        finally:
            with self._lock:
                self._job["question"] = None
                text, self._reply = self._reply, None
        return text

    def answer(self, text) -> dict:
        """提交对当前问题的回答。"""
        if not isinstance(text, str) or not text.strip():
            raise EvaError(Code.INVALID_VALUE, "回答不能为空")
        with self._lock:
            if self._job is None or self._job.get("question") is None:
                raise EvaError(Code.FILE_NOT_FOUND, "当前没有等待回答的问题")
            if self._answered.is_set():
                raise EvaError(Code.INVALID_VALUE, "这个问题已经回答过了")
            self._reply = text.strip()
            self._answered.set()
            return copy.deepcopy(self._job)

    def _work(self, cases: List[Case], runs: int, tenant_name, label, overrides, interaction: str, cancel: threading.Event) -> None:
        try:
            answer = self._human if interaction == EvalInteraction.HUMAN.value else None
            self._run(cases, runs, tenant_name, self._progress, cancel, self._out_root, label, overrides, answer,
                      simulate=interaction == EvalInteraction.MODEL.value)
            state, error = ("cancelled" if cancel.is_set() else "done"), None
        except EvaError as e:
            state, error = "failed", e.to_dict()
        except Exception as e:  # 线程里的未预期异常不能丢：记日志带堆栈，并把类型和消息交给页面
            log.exception("评测任务异常退出")
            state, error = "failed", {"code": None, "name": type(e).__name__, "message": str(e)}
        with self._lock:
            self._job.update(state=state, error=error)

    def current(self) -> Optional[dict]:
        with self._lock:
            return copy.deepcopy(self._job)

    def cancel(self) -> dict:
        """不再开始新的一次；进行中的这次在步骤边界收尾。"""
        with self._lock:
            if self._job is None or self._job["state"] != "running":
                raise EvaError(Code.FILE_NOT_FOUND, "当前没有在跑的评测任务")
            self._cancel.set()
            self._job["cancelling"] = True
            return copy.deepcopy(self._job)

    def join(self, timeout: float = 10) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
