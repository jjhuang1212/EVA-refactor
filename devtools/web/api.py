"""EVA测评系统的接口。路由里只做取参、校验和转发，所有失败都带错误码返回，不吞。"""
import json
import logging
import secrets
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from flask import Flask, Response, request, send_from_directory, session
from werkzeug.exceptions import HTTPException

from devtools.eval import compare, results
from devtools.eval.case import CASES
from devtools.eval.runner import OUT
from devtools.eval.snapshot import ROOT
from devtools.web import cases, data, files
from devtools.web.auth import Auth
from devtools.web.config import WebConfig
from devtools.web.jobs import Jobs
from eva.contracts.errors import Code, EvaError

log = logging.getLogger(__name__)
_STATUS = {Code.LOGIN_REQUIRED: 401, Code.FILE_NOT_FOUND: 404, Code.SESSION_NOT_FOUND: 404,
           Code.TENANT_NOT_FOUND: 404, Code.EVAL_BUSY: 409, Code.FILE_CHANGED: 409, Code.FILE_UNSUPPORTED: 415, Code.CONFIG_INVALID: 400, Code.INVALID_VALUE: 400,
           Code.TENANT_REQUIRED: 400, Code.STORAGE_UNAVAILABLE: 503}


def _default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, (Decimal, Path)):
        return str(o)
    raise TypeError(f"不能序列化 {type(o).__name__}")


def _reply(payload, status: int = 200) -> Response:
    return Response(json.dumps(payload, ensure_ascii=False, default=_default), status, mimetype="application/json")


def _text(source, key: str, required: bool = True) -> Optional[str]:
    value = source.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise EvaError(Code.INVALID_VALUE, f"缺少参数 {key}", param=key)
        return None
    if not isinstance(value, str):
        raise EvaError(Code.INVALID_VALUE, f"参数 {key} 必须是字符串", param=key)
    return value.strip()


def _number(source, key: str, kind):
    raw = _text(source, key, required=False)
    if raw is None:
        return None
    try:
        return kind(raw)
    except ValueError as e:
        raise EvaError(Code.INVALID_VALUE, f"参数 {key} 不是合法的数字", param=key, value=raw) from e


def make_app(cfg: WebConfig, auth: Auth, jobs: Jobs, secret: Optional[bytes] = None, backend=data,
             cases_root: Path = CASES, out_root: Path = OUT, files_root: Path = ROOT) -> Flask:
    app = Flask(__name__)
    app.secret_key = secret or secrets.token_bytes(32)  # 每次启动重新生成，重启后所有人要重新登录
    app.config.update(SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_HTTPONLY=True,
                      PERMANENT_SESSION_LIFETIME=cfg.session_ttl_seconds, MAX_CONTENT_LENGTH=cfg.max_upload_mb << 20)

    @app.before_request
    def require_login():
        if request.path.startswith("/api/") and request.path != "/api/login":
            auth.check(session.get("at"))

    @app.errorhandler(EvaError)
    def eva_error(e: EvaError):
        status = 429 if e.details.get("reason") == "locked" else _STATUS.get(e.code, 500)
        return _reply({"error": e.to_dict()}, status)

    @app.errorhandler(Exception)
    def unexpected(e: Exception):
        if isinstance(e, HTTPException):
            return _reply({"error": {"code": None, "name": e.name, "message": e.description}}, e.code)
        log.exception("接口出现未预期的异常 %s %s", request.method, request.path)
        return _reply({"error": {"code": None, "name": type(e).__name__, "message": str(e)}}, 500)

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.post("/api/login")
    def login():
        at = auth.login(request.remote_addr or "", (request.get_json(silent=True) or {}).get("password"))
        session.permanent = True
        session["at"] = at
        return _reply({"ok": True})

    @app.post("/api/logout")
    def logout():
        session.clear()
        return _reply({"ok": True})

    @app.get("/api/me")
    def me():
        return _reply({"ok": True})

    def _page():
        p = _number(request.args, "page", int)
        if p is None:
            return 1
        if p < 1:
            raise EvaError(Code.INVALID_VALUE, "page 必须是正整数", param="page", value=p)
        return p

    @app.get("/api/tenants")
    def tenants():
        page = _page()
        rows, more = backend.tenants(_text(request.args, "prefix", False), cfg.tenants_limit,
                                     _text(request.args, "search", False), (page - 1) * cfg.tenants_limit)
        return _reply({"items": rows, "truncated": more, "has_more": more, "page": page, "limit": cfg.tenants_limit})

    @app.get("/api/sessions")
    def sessions():
        page = _page()
        rows, more = backend.sessions(_text(request.args, "tenant"), cfg.sessions_limit, (page - 1) * cfg.sessions_limit)
        return _reply({"items": rows, "truncated": more, "has_more": more, "page": page, "limit": cfg.sessions_limit})

    @app.get("/api/replay")
    def replay():
        return _reply(backend.replay(_text(request.args, "tenant"), _text(request.args, "session"),
                                     request.args.get("full") == "1"))

    @app.get("/api/cases")
    def list_cases():
        return _reply({"items": cases.list_cases(cases_root)})

    @app.post("/api/cases")
    def create_case():
        f = request.form
        files = [(u.filename or "", u.read()) for u in request.files.getlist("files")]
        return _reply(cases.create_case(
            _text(f, "name"), _text(f, "message"), files,
            [w.strip() for w in f.getlist("reply_contains") if w.strip()],
            [w.strip() for w in f.getlist("reply_not_contains") if w.strip()], _number(f, "max_steps", int),
            _number(f, "max_cost_cny", float), _number(f, "max_questions", int), _text(f, "persona", False), cases_root), 201)

    @app.post("/api/runs")
    def start_run():
        body = request.get_json(silent=True) or {}
        names = body.get("cases")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise EvaError(Code.INVALID_VALUE, "cases 必须是案例名的列表", param="cases")
        runs = body.get("runs", 1)
        return _reply(jobs.start([cases.get_case(n.strip(), cases_root) for n in names], runs, _text(body, "tenant", False),
                                 _text(body, "label", False), body.get("overrides"), body.get("interaction", "none")), 202)

    @app.get("/api/defaults")
    def defaults():
        return _reply(backend.defaults())

    @app.post("/api/runs/current/answer")
    def answer_question():
        return _reply(jobs.answer((request.get_json(silent=True) or {}).get("text")))

    @app.get("/api/runs/current")
    def current_run():
        return _reply({"job": jobs.current()})

    @app.get("/api/runs/stream")
    def runs_stream():
        """评测进度实时推送：状态变化才发一条，任务结束（或没有任务）就关闭连接。
        只推 job 快照，前端拿到后自行渲染；鉴权走 before_request。"""
        def gen():
            last = None
            while True:
                job = jobs.current()
                payload = json.dumps(job, ensure_ascii=False, default=_default)
                if payload != last:
                    last = payload
                    yield f"data: {payload}\n\n"
                if not job or job.get("state") != "running":
                    return
                time.sleep(0.5)
        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/current/cancel")
    def cancel_run():
        return _reply(jobs.cancel())

    @app.get("/api/results")
    def list_results():
        page = _page()
        case = _text(request.args, "case", False)
        rows, more = results.list_results(case, cfg.results_limit, out_root, (page - 1) * cfg.results_limit)
        if case is not None:  # 按案例看时，每行带当前生效的人工判定（没标过是 null）
            verdicts = backend.human_verdicts(case)
            rows = [{**r, "human": verdicts.get(r["run_id"])} for r in rows]
        return _reply({"items": rows, "truncated": more, "has_more": more, "page": page, "limit": cfg.results_limit})

    @app.get("/api/labels")
    def list_labels():
        return _reply({"items": results.list_labels(_text(request.args, "case"), out_root)})

    @app.post("/api/results/<case>/<run_id>/annotation")
    def annotate(case: str, run_id: str):
        body = request.get_json(silent=True) or {}
        return _reply(backend.annotate(case, run_id, _text(body, "verdict"), _text(body, "note", False), out_root), 201)

    @app.get("/api/versions")
    def versions():
        return _reply({"items": backend.versions()})

    @app.post("/api/versions")
    def mark_version():
        body = request.get_json(silent=True) or {}
        return _reply(backend.mark_version(_text(body, "case"), _text(body, "run_id"), _text(body, "name"),
                                           _text(body, "note", False), out_root), 201)

    @app.get("/api/versions/<name>")
    def version(name: str):
        return _reply(backend.version(name))

    @app.get("/api/files")
    def list_files():
        return _reply({"items": files.list_files(files_root)})

    @app.get("/api/files/<path:rel>")
    def read_file(rel: str):
        return _reply(files.read_file(rel, files_root))

    def require_idle():
        job = jobs.current()
        if job is not None and job["state"] == "running":  # 评测读的就是这些文件，跑的时候改会让结果不可比
            raise EvaError(Code.EVAL_BUSY, "评测在跑，结束或取消后再保存", job_id=job["job_id"])

    @app.put("/api/files/<path:rel>")
    def write_file(rel: str):
        require_idle()
        body = request.get_json(silent=True) or {}
        return _reply(files.write_file(rel, body.get("content"), body.get("base_sha"), files_root))

    @app.get("/api/results/<case>/<run_id>")
    def read_result(case: str, run_id: str):
        res = results.read_result(case, run_id, out_root)
        return _reply({**res, "human": backend.human_verdicts(case).get(run_id)})

    @app.post("/api/results/<case>/<run_id>/adopt")
    def adopt(case: str, run_id: str):
        require_idle()
        res = results.read_result(case, run_id, out_root)
        return _reply({"adopted": files.adopt(res.get("overrides") or {}, res.get("overrides_base") or {}, files_root)})

    @app.get("/api/results/<case>/<run_id>/files/<path:name>")
    def result_file(case: str, run_id: str, name: str):
        return send_from_directory(str(results.run_dir(case, run_id, out_root)), name, as_attachment=True)

    @app.get("/api/compare")
    def compare_runs():
        case = _text(request.args, "case")
        return _reply(compare.compare(case, _text(request.args, "a"), _text(request.args, "b"), out_root,
                                      human=backend.human_verdicts(case)))

    return app
