"""模拟器的网页服务：只监听本机，没有鉴权。"""
import mimetypes
import uuid
from pathlib import Path
from typing import Callable, List, Tuple
from urllib.parse import quote

from flask import Flask, Response, jsonify, request, send_from_directory

from eva.channels.wecom.config import WecomConfig
from eva.contracts.errors import Code, EvaError
from tools.wecom_sim import history
from tools.wecom_sim.sim import USER_PREFIX, Mailbox

_HERE = Path(__file__).resolve().parent
_IMAGE_TYPES = ("image/jpeg", "image/png")


def make_app(box: Mailbox, cfg: WecomConfig, default_kf: str,
             read_history: Callable[[str, str], List[dict]] = history.read,
             known: Callable[[], List[dict]] = history.known,
             read_file: Callable[[str, str, str], Tuple[bytes, str, str]] = history.read_file) -> Flask:
    app = Flask(__name__)
    boot = uuid.uuid4().hex  # 消息记录在内存里，重启后编号重来；页面靠它发现服务换了一轮

    @app.errorhandler(EvaError)
    def eva_error(e: EvaError):
        """不合规的用户 ID 是调用方的错（400），文件不存在是 404，其余（存储不可用等）是服务的错（502）。"""
        status = 400 if e.code is Code.INVALID_VALUE else 404 if e.code is Code.FILE_NOT_FOUND else 502
        return jsonify(error=f"{e.code.value} {e.message}"), status

    @app.get("/")
    def index():
        return send_from_directory(str(_HERE), "index.html")

    @app.get("/api/config")
    def config():
        return jsonify(kf=default_kf, user_prefix=USER_PREFIX, file_limit_mb=cfg.file_limit_mb,
                       image_limit_mb=cfg.image_limit_mb)

    @app.get("/api/conversations")
    def conversations():
        """内存里的会话在前（有最近消息的时间），库里有、内存里没有的（重启前的）排在后面。"""
        live = box.conversations()
        seen = {(r["kf"], r["user"]) for r in live}
        old = [{**r, "count": 0, "preview": "", "busy": False} for r in known() if (r["kf"], r["user"]) not in seen]
        return jsonify(items=live + old)

    @app.post("/api/send")
    def send():
        kf, user = request.form.get("kf", "").strip(), request.form.get("user", "").strip()
        if not kf or not user:
            return jsonify(error="缺少客服号或用户"), 400
        upload = request.files.get("file")
        if upload is None:
            text = request.form.get("text", "")
            if not text.strip():
                return jsonify(error="消息为空"), 400
            box.user_text(kf, user, text, request.form.get("menu_id") or None)
            return jsonify(ok=True)
        data = upload.read()
        if not data:
            return jsonify(error="文件是空的"), 400
        ctype = upload.mimetype if upload.mimetype != "application/octet-stream" else (
            mimetypes.guess_type(upload.filename or "")[0] or "application/octet-stream")
        kind = request.form.get("kind") or ("image" if ctype in _IMAGE_TYPES else "file")
        box.user_media(kf, user, kind, data, ctype, upload.filename or "upload.bin",
                       hide_name=request.form.get("hide_name") == "1",
                       force_oversize=request.form.get("oversize") == "1")
        return jsonify(ok=True)

    @app.get("/api/poll")
    def poll():
        kf, user = request.args.get("kf", ""), request.args.get("user", "")
        items, busy = box.feed(kf, user, int(request.args.get("since") or 0))
        return jsonify(boot=boot, items=items, busy=busy, alerts=box.alerts_since(int(request.args.get("alerts") or 0)))

    @app.get("/api/history")
    def history_of():
        """内存里还有这个对话时不返回：内存的记录更全，且避免和历史重复。"""
        kf, user = request.args.get("kf", ""), request.args.get("user", "")
        if not kf or not user:
            return jsonify(error="缺少客服号或用户"), 400
        if box.feed(kf, user, 0)[0]:
            return jsonify(items=[])
        return jsonify(items=read_history(kf, user))

    @app.get("/api/stored/<file_id>")
    def stored(file_id: str):
        """重启后还原出来的文件气泡：内容从文件登记取，不在内存里。"""
        data, ctype, name = read_file(request.args.get("kf", ""), request.args.get("user", ""), file_id)
        return Response(data, mimetype=ctype, headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}"})

    @app.get("/api/media/<media_id>")
    def media(media_id: str):
        item = box.media_bytes(media_id)
        if item is None:
            return jsonify(error="素材不存在"), 404
        return Response(item[0], mimetype=item[1], headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(box.media_filename(media_id) or media_id)}"})

    return app
