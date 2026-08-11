import os
import secrets
from datetime import datetime, timedelta, timezone
from threading import RLock

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.services.memo_service import (
    create_memo,
    create_or_get_video,
    delete_memo,
    delete_video,
    embed_url,
    get_video,
    list_memos,
    list_videos,
    search_videos_and_memos,
    update_memo,
)
from app.services.host_urls import portal_home_url, request_host_from_headers

app = FastAPI(title="Youtube Memo")

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_PUBLIC_ORIGIN = "https://memo.len.pe.kr"
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://img.youtube.com https://image.aladin.co.kr "
        "https://books.google.com https://covers.openlibrary.org; "
        "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'; frame-src 'self' https://www.youtube.com"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def _request_origin(request: Request) -> str:
    if request.url.hostname == "memo.len.pe.kr":
        return _PUBLIC_ORIGIN
    return str(request.base_url).rstrip("/")


@app.middleware("http")
async def apply_browser_security(request: Request, call_next):
    if request.method not in _SAFE_METHODS:
        expected_origin = _request_origin(request)
        if request.headers.get("origin") != expected_origin:
            response = JSONResponse(
                status_code=403,
                content={"detail": "Cross-origin requests are not allowed."},
            )
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)

    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value
    return response

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
AUTH_RATE_LIMIT_MAX_FAILURES = int(os.getenv("AUTH_RATE_LIMIT_MAX_FAILURES", "5"))
AUTH_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "300"))
_AUTH_FAILURES: dict[str, list[datetime]] = {}
WRITE_AUTH_COOKIE = "youtube_memo_write_session"
WRITE_AUTH_MAX_AGE = int(os.getenv("MEMO_WRITE_SESSION_MAX_AGE", str(8 * 60 * 60)))
AUTH_SESSION_MAX_ENTRIES = max(1, int(os.getenv("AUTH_SESSION_MAX_ENTRIES", "1024")))
_WRITE_SESSIONS: dict[str, datetime] = {}
_WRITE_SESSIONS_LOCK = RLock()


def _portal_home_url(request: Request) -> str:
    return portal_home_url(request_host_from_headers(request.headers))


templates.env.globals["portal_home_url_for_request"] = _portal_home_url


@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "title": "유튜브 메모장",
            "videos": list_videos(),
            "portal_home_url": portal_home_url(request_host_from_headers(request.headers)),
            "write_authenticated": _has_write_session(request),
        },
    )


@app.post("/videos")
def create_video(request: Request, url: str = Form(...)):
    _require_write_session(request)
    try:
        video = create_or_get_video(url)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return RedirectResponse(
        url=f"/videos/{video['id']}",
        status_code=303,
    )


@app.get("/videos/{video_id}")
def video_detail(request: Request, video_id: int):
    video = get_video(video_id)

    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    return templates.TemplateResponse(
        "video_detail.html",
        {
            "request": request,
            "title": video["title"],
            "video": video,
            "embed_url": embed_url(video["youtube_id"]),
            "memos": list_memos(video_id),
            "portal_home_url": portal_home_url(request_host_from_headers(request.headers)),
            "write_authenticated": _has_write_session(request),
        },
    )


@app.post("/videos/{video_id}/memos")
def create_video_memo(
    request: Request,
    video_id: int,
    memo_title: str = Form(default=""),
    content: str = Form(...),
):
    _require_write_session(request)
    if not get_video(video_id):
        raise HTTPException(status_code=404, detail="Video not found")

    try:
        create_memo(video_id=video_id, title=memo_title, content=content)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return RedirectResponse(
        url=f"/videos/{video_id}",
        status_code=303,
    )


@app.post("/videos/{video_id}/delete")
def delete_saved_video(
    request: Request,
    video_id: int,
    delete_password: str = Form(default=""),
):
    _require_write_session(request)
    _require_delete_password(request, delete_password)

    deleted = delete_video(video_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Video not found")

    return RedirectResponse(
        url="/",
        status_code=303,
    )


@app.post("/memos/{memo_id}/delete")
def delete_video_memo(
    request: Request,
    memo_id: int,
    delete_password: str = Form(default=""),
):
    _require_write_session(request)
    _require_delete_password(request, delete_password)

    video_id = delete_memo(memo_id)

    if not video_id:
        raise HTTPException(status_code=404, detail="Memo not found")

    return RedirectResponse(
        url=f"/videos/{video_id}",
        status_code=303,
    )


@app.post("/memos/{memo_id}")
def update_video_memo(
    request: Request,
    memo_id: int,
    memo_title: str = Form(default=""),
    content: str = Form(...),
    edit_password: str = Form(default=""),
):
    _require_write_session(request)
    _require_delete_password(request, edit_password)

    try:
        video_id = update_memo(memo_id=memo_id, title=memo_title, content=content)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    if not video_id:
        raise HTTPException(status_code=404, detail="Memo not found")

    return RedirectResponse(
        url=f"/videos/{video_id}",
        status_code=303,
    )


@app.get("/health")
def health():
    return {
        "service": "youtube-memo",
        "status": "ok",
    }


@app.get("/api/search")
def search_api(q: str = "", limit: int = 5):
    limit = max(1, min(limit, 20))
    return {
        "results": search_videos_and_memos(q, limit=limit),
    }


@app.get("/auth/login")
def write_login_page(request: Request, next_path: str = ""):
    return _write_login_response(request, next_path=_safe_redirect(next_path))


@app.post("/auth/login")
def write_login(request: Request, password: str = Form(default=""), next_path: str = Form(default="")):
    client = _client_id(request)
    safe_next_path = _safe_redirect(next_path)
    if _auth_rate_limited(client):
        return _write_login_response(request, "비밀번호 실패가 반복되어 잠시 후 다시 시도해주세요.", 429, safe_next_path)

    configured_password = os.getenv("DELETE_PASSWORD", "").strip()
    if not configured_password:
        return _write_login_response(request, "쓰기 비밀번호가 설정되지 않았습니다.", 403, safe_next_path)

    if not secrets.compare_digest(password, configured_password):
        _record_auth_failure(client)
        return _write_login_response(request, "비밀번호가 올바르지 않습니다.", 403, safe_next_path)

    _clear_auth_failures(client)
    response = RedirectResponse(url=safe_next_path or "/", status_code=303)
    response.set_cookie(
        WRITE_AUTH_COOKIE,
        _create_write_session(),
        httponly=True,
        secure=os.getenv("APP_ENV", "").strip().lower() == "production",
        samesite="lax",
        path="/",
        max_age=WRITE_AUTH_MAX_AGE,
    )
    return response


@app.post("/auth/logout")
def write_logout(request: Request, next_path: str = Form(default="")):
    _revoke_write_session(request.cookies.get(WRITE_AUTH_COOKIE, ""))
    response = RedirectResponse(url=_safe_redirect(next_path) or "/", status_code=303)
    response.delete_cookie(WRITE_AUTH_COOKIE, path="/")
    return response


def _require_delete_password(request: Request, password: str) -> None:
    configured_password = os.getenv("DELETE_PASSWORD", "").strip()
    client = _client_id(request)

    if _auth_rate_limited(client):
        raise HTTPException(status_code=429, detail="비밀번호 실패가 반복되어 잠시 후 다시 시도해주세요.")

    if not configured_password:
        raise HTTPException(status_code=403, detail="삭제 비밀번호가 설정되지 않았습니다.")

    if not secrets.compare_digest(password, configured_password):
        _record_auth_failure(client)
        raise HTTPException(status_code=403, detail="삭제 비밀번호가 올바르지 않습니다.")
    _clear_auth_failures(client)


def _require_write_session(request: Request) -> None:
    if not _has_write_session(request):
        raise HTTPException(status_code=401, detail="메모 쓰기 인증이 필요합니다.")


def _has_write_session(request: Request) -> bool:
    token = request.cookies.get(WRITE_AUTH_COOKIE, "")
    if not token:
        return False
    with _WRITE_SESSIONS_LOCK:
        _prune_write_sessions()
        return token in _WRITE_SESSIONS


def _create_write_session() -> str:
    with _WRITE_SESSIONS_LOCK:
        _prune_write_sessions()
        while len(_WRITE_SESSIONS) >= AUTH_SESSION_MAX_ENTRIES:
            oldest_token = min(_WRITE_SESSIONS, key=_WRITE_SESSIONS.get)
            _WRITE_SESSIONS.pop(oldest_token, None)
        token = secrets.token_urlsafe(32)
        while token in _WRITE_SESSIONS:
            token = secrets.token_urlsafe(32)
        _WRITE_SESSIONS[token] = datetime.now(timezone.utc) + timedelta(seconds=WRITE_AUTH_MAX_AGE)
        return token


def _revoke_write_session(token: str) -> None:
    if not token:
        return
    with _WRITE_SESSIONS_LOCK:
        _WRITE_SESSIONS.pop(token, None)


def _prune_write_sessions() -> None:
    now = datetime.now(timezone.utc)
    for token in [token for token, expires_at in _WRITE_SESSIONS.items() if expires_at <= now]:
        _WRITE_SESSIONS.pop(token, None)


def _write_login_response(request: Request, error: str = "", status_code: int = 200, next_path: str = ""):
    return templates.TemplateResponse(
        "auth_login.html",
        {
            "request": request,
            "title": "유튜브 메모 쓰기 로그인",
            "error": error,
            "next_path": next_path,
        },
        status_code=status_code,
    )


def _safe_redirect(path: str) -> str:
    if path.startswith("/") and not path.startswith("//"):
        return path
    return ""


def _auth_rate_limited(client: str) -> bool:
    return len(_active_auth_failures(client)) >= AUTH_RATE_LIMIT_MAX_FAILURES


def _record_auth_failure(client: str) -> None:
    failures = _active_auth_failures(client)
    failures.append(datetime.now(timezone.utc))
    _AUTH_FAILURES[client] = failures


def _clear_auth_failures(client: str) -> None:
    _AUTH_FAILURES.pop(client, None)


def _active_auth_failures(client: str) -> list[datetime]:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=AUTH_RATE_LIMIT_WINDOW_SECONDS)
    failures = [failed_at for failed_at in _AUTH_FAILURES.get(client, []) if failed_at >= cutoff]
    _AUTH_FAILURES[client] = failures
    return failures


def _client_id(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded_for:
        return forwarded_for
    return request.client.host if request.client else "unknown"
