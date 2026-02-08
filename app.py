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

# 전역 서비스 (초기화 시 로드)
_tci_cfg: Config | None = None
_tci_db: Database | None = None
_tci_yt: YouTubeService | None = None
_tci_ai: KeywordAI | None = None
_tci_refresh_lock = threading.Lock()
_tci_refresh_running = False


def _tci_get_services():
    global _tci_cfg, _tci_db, _tci_yt, _tci_ai
    if _tci_db is None:
        _tci_cfg = load_config(_TCI_BASE_DIR)
        _tci_db = Database(_tci_cfg)
        _tci_yt = YouTubeService(_tci_cfg.youtube_api_key)
        _tci_ai = KeywordAI(
            _tci_cfg.ai_enabled,
            _tci_cfg.ai_provider,
            _tci_cfg.ai_api_key,
            _tci_cfg.ai_model,
            _tci_cfg.ai_base_url,
        )
    return _tci_cfg, _tci_db, _tci_yt, _tci_ai


def _tci_run_refresh():
    global _tci_refresh_running
    with _tci_refresh_lock:
        if _tci_refresh_running:
            return
        _tci_refresh_running = True
    try:
        cfg, _, _, _ = _tci_get_services()
        worker_db = Database(cfg)
        tci_collect_impl(worker_db, _tci_yt, _tci_ai)
        worker_db.conn.close()
    except Exception:
        pass
    finally:
        with _tci_refresh_lock:
            _tci_refresh_running = False


def _tci_get_totp() -> pyotp.TOTP | None:
    """config에서 TOTP 시크릿을 읽어 pyotp.TOTP 객체 반환. 비어 있으면 None (인증 비활성)."""
    cfg, _, _, _ = _tci_get_services()
    if not cfg.auth_totp_secret:
        return None
    return pyotp.TOTP(cfg.auth_totp_secret)


def tci_login_required(f):
    """로그인 필수 데코레이터. TOTP 시크릿이 설정되어 있으면 세션 검증."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        cfg, _, _, _ = _tci_get_services()
        if cfg.auth_totp_secret and not session.get("tci_authenticated"):
            return redirect(url_for("auth_login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/auth/login", methods=["GET", "POST"])
def auth_login():
    """TOTP 6자리 코드 입력 로그인 페이지."""
    cfg, _, _, _ = _tci_get_services()
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
    cfg, _, _, _ = _tci_get_services()
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
    _, db, _, _ = _tci_get_services()
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
    return render_template(
        "index.html",
        videos=videos,
        keywords=keywords,
        suggest_words=suggest_words,
        query=query,
        status=status,
        live_video_id=None,
        page=page,
        per_page=_PER_PAGE,
        total=total,
        total_pages=total_pages,
    )


@app.route("/api/refresh", methods=["POST"])
@tci_login_required
def api_refresh():
    t = threading.Thread(target=_tci_run_refresh, daemon=True)
    t.start()
    if request.headers.get("Accept", "").find("application/json") >= 0:
        return jsonify({"ok": True, "message": "수집을 시작했습니다. 잠시 후 새로고침 하세요."})
    return redirect(url_for("index") + "?refresh=started")


@app.route("/api/videos")
@tci_login_required
def api_videos():
    _, db, _, _ = _tci_get_services()
    query = request.args.get("query", "").strip()
    status = request.args.get("status", "").strip()
    rows = db.list_videos(query, status)
    return jsonify([{"video_id": r[0], "title": r[1], "channel_id": r[2], "published_at": r[3], "status": r[4]} for r in rows])


@app.route("/api/keywords", methods=["GET"])
@tci_login_required
def api_keywords_list():
    _, db, _, _ = _tci_get_services()
    return jsonify(db.active_keywords()[:200])


@app.route("/api/keywords", methods=["POST"])
@tci_login_required
def api_keywords_add():
    _, db, _, _ = _tci_get_services()
    data = request.get_json(silent=True) or {}
    keyword = (data.get("keyword") or request.form.get("keyword", "")).strip().lower()
    if keyword:
        db.save_ai_keywords([keyword])
    if request.headers.get("Accept", "").find("application/json") >= 0:
        return jsonify({"ok": True})
    return redirect(url_for("index"))


@app.route("/api/keywords/<keyword>", methods=["DELETE"])
@tci_login_required
def api_keywords_delete(keyword):
    _, db, _, _ = _tci_get_services()
    db.delete_keyword(keyword)
    return jsonify({"ok": True})


@app.route("/api/videos/<video_id>/status", methods=["POST"])
@tci_login_required
def api_video_status(video_id):
    _, db, _, _ = _tci_get_services()
    data = request.get_json(silent=True) or request.form
    status = (data.get("status") or "").strip().upper()
    if status in ("UNWATCHED", "WATCHING", "WATCHED"):
        db.set_watch_status(video_id, status)
    return jsonify({"ok": True})


@app.route("/api/videos/<video_id>/hide", methods=["POST"])
@tci_login_required
def api_video_hide(video_id):
    _, db, _, _ = _tci_get_services()
    db.hide_video(video_id)
    return jsonify({"ok": True})


if __name__ == "__main__":
    _tci_get_services()
    app.run(host="0.0.0.0", port=5000, debug=True)
