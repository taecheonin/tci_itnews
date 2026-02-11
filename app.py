# IT News Collector - Flask 웹 앱
from __future__ import annotations

import base64
import functools
import io
import os
import threading

import pyotp
import qrcode
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from tci_core import (
    Config,
    Database,
    KeywordAI,
    YouTubeService,
    load_config,
    tci_collect_impl,
)

_TCI_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(_TCI_BASE_DIR, "templates"))
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "tci-itnews-dev-secret")
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 7  # 7일간 세션 유지

# 전역: 설정·YouTube·AI만 보관 (DB 커넥션은 요청마다 새로 생성)
_tci_cfg: Config | None = None
_tci_yt: YouTubeService | None = None
_tci_ai: KeywordAI | None = None
_tci_refresh_lock = threading.Lock()
_tci_refresh_running = False


def _tci_get_services():
    """설정·YouTube·AI 서비스를 로드 (최초 1회). DB는 포함하지 않음."""
    global _tci_cfg, _tci_yt, _tci_ai
    if _tci_cfg is None:
        _tci_cfg = load_config(_TCI_BASE_DIR)
        _tci_yt = YouTubeService(_tci_cfg.youtube_api_key)
        _tci_ai = KeywordAI(
            _tci_cfg.ai_enabled,
            _tci_cfg.ai_provider,
            _tci_cfg.ai_api_key,
            _tci_cfg.ai_model,
            _tci_cfg.ai_base_url,
        )
    return _tci_cfg, _tci_yt, _tci_ai


def _tci_new_db() -> Database:
    """요청마다 새 DB 커넥션 생성. 사용 후 반드시 db.close() 호출."""
    cfg, _, _ = _tci_get_services()
    return Database(cfg)


def _tci_run_refresh():
    """백그라운드 수집 스레드. 전용 DB 커넥션 사용."""
    global _tci_refresh_running
    with _tci_refresh_lock:
        if _tci_refresh_running:
            return
        _tci_refresh_running = True
    try:
        _, yt, ai = _tci_get_services()
        worker_db = _tci_new_db()
        try:
            tci_collect_impl(worker_db, yt, ai)
        finally:
            worker_db.close()
    except Exception:
        pass
    finally:
        with _tci_refresh_lock:
            _tci_refresh_running = False


def tci_login_required(f):
    """로그인 필수 데코레이터. TOTP 시크릿이 설정되어 있으면 세션 검증."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        cfg, _, _ = _tci_get_services()
        if cfg.auth_totp_secret and not session.get("tci_authenticated"):
            return redirect(url_for("auth_login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/auth/login", methods=["GET", "POST"])
def auth_login():
    """TOTP 6자리 코드 입력 로그인 페이지."""
    cfg, _, _ = _tci_get_services()
    if not cfg.auth_totp_secret:
        return redirect(url_for("index"))
    if session.get("tci_authenticated"):
        return redirect(url_for("index"))
    error = ""
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        totp = pyotp.TOTP(cfg.auth_totp_secret)
        if totp.verify(code, valid_window=1):
            session.permanent = True
            session["tci_authenticated"] = True
            return redirect(url_for("index"))
        error = "인증 코드가 올바르지 않습니다."
    return render_template("login.html", error=error)


@app.route("/auth/logout")
def auth_logout():
    """로그아웃: 세션 초기화 후 로그인 페이지로 이동."""
    session.clear()
    return redirect(url_for("auth_login"))


@app.route("/auth/setup")
def auth_setup():
    """Google Authenticator QR 코드 표시 (최초 등록용)."""
    cfg, _, _ = _tci_get_services()
    if not cfg.auth_totp_secret:
        return "config.toml [auth] totp_secret을 먼저 설정하세요.", 400
    totp = pyotp.TOTP(cfg.auth_totp_secret)
    provisioning_uri = totp.provisioning_uri(
        name="admin",
        issuer_name=cfg.auth_app_name,
    )
    img = qrcode.make(provisioning_uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return render_template(
        "setup.html",
        qr_b64=qr_b64,
        secret=cfg.auth_totp_secret,
        app_name=cfg.auth_app_name,
    )


_PER_PAGE = 20


@app.route("/")
@tci_login_required
def index():
    db = _tci_new_db()
    try:
        db.ensure_seed("it")
        query = request.args.get("query", "").strip()
        status = request.args.get("status", "").strip()
        page = max(1, int(request.args.get("page", 1)))
        total = db.list_videos_count(query, status)
        total_pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE) if total else 1
        page = min(page, total_pages)
        offset = (page - 1) * _PER_PAGE
        videos = db.list_videos(query, status, limit=_PER_PAGE, offset=offset)
        keywords = db.active_keywords()[:200]
        suggest_words = db.all_suggest_words()
        channels = db.list_channels()
        return render_template(
            "index.html",
            videos=videos,
            keywords=keywords,
            suggest_words=suggest_words,
            channels=channels,
            query=query,
            status=status,
            page=page,
            per_page=_PER_PAGE,
            total=total,
            total_pages=total_pages,
        )
    finally:
        db.close()


@app.route("/api/refresh", methods=["POST"])
@tci_login_required
def api_refresh():
    t = threading.Thread(target=_tci_run_refresh, daemon=True)
    t.start()
    if request.headers.get("Accept", "").find("application/json") >= 0:
        return jsonify({"ok": True, "message": "가장 오래된 키워드·채널을 수집 중입니다. 잠시 후 새로고침 하세요."})
    return redirect(url_for("index") + "?refresh=started")


@app.route("/api/videos")
@tci_login_required
def api_videos():
    db = _tci_new_db()
    try:
        query = request.args.get("query", "").strip()
        status = request.args.get("status", "").strip()
        rows = db.list_videos(query, status)
        return jsonify(
            [{"video_id": r[0], "title": r[1], "channel_id": r[2], "published_at": r[3], "status": r[4]} for r in rows]
        )
    finally:
        db.close()


@app.route("/api/keywords", methods=["GET"])
@tci_login_required
def api_keywords_list():
    db = _tci_new_db()
    try:
        return jsonify(db.active_keywords()[:200])
    finally:
        db.close()


@app.route("/api/keywords", methods=["POST"])
@tci_login_required
def api_keywords_add():
    db = _tci_new_db()
    try:
        data = request.get_json(silent=True) or {}
        keyword = (data.get("keyword") or request.form.get("keyword", "")).strip().lower()
        if keyword:
            db.save_manual_keywords([keyword])
        if request.headers.get("Accept", "").find("application/json") >= 0:
            return jsonify({"ok": True})
        return redirect(url_for("index"))
    finally:
        db.close()


@app.route("/api/keywords/<keyword>", methods=["DELETE"])
@tci_login_required
def api_keywords_delete(keyword):
    db = _tci_new_db()
    try:
        db.delete_keyword(keyword)
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/videos/<video_id>/status", methods=["POST"])
@tci_login_required
def api_video_status(video_id):
    db = _tci_new_db()
    try:
        data = request.get_json(silent=True) or request.form
        status = (data.get("status") or "").strip().upper()
        if status in ("UNWATCHED", "WATCHING", "WATCHED"):
            db.set_watch_status(video_id, status)
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/videos/<video_id>/tags", methods=["GET"])
@tci_login_required
def api_video_tags(video_id):
    db = _tci_new_db()
    try:
        tags = db.get_video_tags(video_id)
        return jsonify(tags)
    finally:
        db.close()


@app.route("/api/videos/<video_id>/hide", methods=["POST"])
@tci_login_required
def api_video_hide(video_id):
    db = _tci_new_db()
    try:
        db.hide_video(video_id)
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/channels", methods=["GET"])
@tci_login_required
def api_channels_list():
    db = _tci_new_db()
    try:
        rows = db.list_channels()
        result = [
            {
                "channel_id": row[0],
                "title": row[1],
                "created_date": row[2].isoformat() if hasattr(row[2], "isoformat") else str(row[2]),
                "updated_date": row[3].isoformat() if hasattr(row[3], "isoformat") else str(row[3]),
            }
            for row in rows
        ]
        return jsonify(result)
    finally:
        db.close()


@app.route("/api/channels", methods=["POST"])
@tci_login_required
def api_channels_add():
    db = _tci_new_db()
    try:
        data = request.get_json(silent=True) or request.form
        channel_id = (data.get("channel_id") or "").strip()
        if not channel_id:
            return jsonify({"ok": False, "error": "채널 ID를 입력하세요."}), 400
        _, yt, _ = _tci_get_services()
        meta = yt.channel_details(channel_id)
        if meta is None:
            return jsonify({"ok": False, "error": "채널을 찾을 수 없습니다."}), 404
        title = meta.get("title", channel_id)
        db.save_channel(channel_id, title)
        return jsonify({"ok": True, "channel": {"channel_id": channel_id, "title": title}})
    finally:
        db.close()


@app.route("/api/channels/<channel_id>", methods=["DELETE"])
@tci_login_required
def api_channels_delete(channel_id):
    db = _tci_new_db()
    try:
        db.delete_channel(channel_id)
        return jsonify({"ok": True})
    finally:
        db.close()


if __name__ == "__main__":
    _tci_get_services()
    app.run(host="0.0.0.0", port=5002, debug=False)
