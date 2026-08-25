import json
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from urllib.parse import quote, urlsplit

import fcntl

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.services.book_search import search_books
from app.services.datetime_format import format_display_datetime
from app.services.book_service import (
    DB_PATH,
    create_chapter,
    create_chapters,
    create_memo,
    create_or_get_book,
    delete_book,
    delete_chapter,
    delete_memo,
    get_book,
    list_books,
    list_chapters,
    list_memos,
    search_books_and_memos,
    update_chapter,
    update_chapter_comment,
    update_chapter_statuses,
    update_progress,
)
from app.services.toc_service import fetch_toc_candidates
from app.services.host_urls import portal_home_url, request_host_from_headers


app = FastAPI(title="Book Memo")

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_PUBLIC_ORIGIN = "https://books.len.pe.kr"
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
    if request.url.hostname == "books.len.pe.kr":
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
templates.env.filters["display_datetime"] = format_display_datetime
AUTH_RATE_LIMIT_MAX_FAILURES = int(os.getenv("AUTH_RATE_LIMIT_MAX_FAILURES", "5"))
AUTH_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "300"))
AUTH_RATE_LIMIT_STATE_PATH = Path(
    os.getenv("AUTH_RATE_LIMIT_STATE_PATH", "").strip()
    or DB_PATH.parent / "auth-rate-limit-state.json"
)
_AUTH_FAILURES: dict[str, list[datetime]] = {}
WRITE_AUTH_COOKIE = "book_memo_write_session"
WRITE_AUTH_MAX_AGE = int(os.getenv("MEMO_WRITE_SESSION_MAX_AGE", str(8 * 60 * 60)))
AUTH_SESSION_MAX_ENTRIES = max(1, int(os.getenv("AUTH_SESSION_MAX_ENTRIES", "1024")))
_WRITE_SESSIONS: dict[str, datetime] = {}
_WRITE_SESSIONS_LOCK = RLock()


def _portal_home_url(request: Request) -> str:
    return portal_home_url(request_host_from_headers(request.headers))


templates.env.globals["portal_home_url_for_request"] = _portal_home_url


@app.get("/")
def home(
    request: Request,
    q: str = Query(default=""),
):
    results = []
    error = ""

    if q.strip():
        try:
            results = search_books(q)
        except Exception as exc:
            error = str(exc)

    books = list_books()

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "title": "책 메모장",
            "query": q,
            "results": results,
            "books": books,
            "chapters_by_book": {book["id"]: list_chapters(book["id"]) for book in books},
            "error": error,
            "has_aladin_key": bool(os.getenv("ALADIN_TTB_KEY", "").strip()),
            "statuses": ["읽을 예정", "읽는 중", "완료", "보류"],
            "portal_home_url": portal_home_url(request_host_from_headers(request.headers)),
            "write_authenticated": _has_write_session(request),
        },
    )


@app.post("/books")
def create_book(
    request: Request,
    isbn: str = Form(...),
    external_id: str = Form(default=""),
    title: str = Form(...),
    authors: str = Form(default=""),
    publisher: str = Form(default=""),
    published_date: str = Form(default=""),
    description: str = Form(default=""),
    thumbnail: str = Form(default=""),
    preview_url: str = Form(default=""),
    source: str = Form(default="google_books"),
    titles: list[str] = Form(default=[]),
):
    _require_write_session(request)
    book = create_or_get_book(
        {
            "isbn": isbn,
            "external_id": external_id,
            "title": title,
            "authors": authors,
            "publisher": publisher,
            "published_date": published_date,
            "description": description,
            "thumbnail": thumbnail,
            "preview_url": preview_url,
            "source": source,
        }
    )

    if titles:
        create_chapters(book_id=book["id"], titles=titles)

    return RedirectResponse(url=f"/books/{book['id']}", status_code=303)


@app.post("/toc-candidates")
def get_unsaved_book_toc_candidates(
    isbn: str = Form(default=""),
    title: str = Form(default=""),
):
    return fetch_toc_candidates(
        {
            "isbn": isbn,
            "title": title,
        }
    )


@app.get("/books/{book_id}")
def book_detail(request: Request, book_id: int):
    book = get_book(book_id)

    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    return templates.TemplateResponse(
        "book_detail.html",
        {
            "request": request,
            "title": book["title"],
            "book": book,
            "chapters": list_chapters(book_id),
            "memos": list_memos(book_id),
            "statuses": ["읽을 예정", "읽는 중", "완료", "보류"],
            "portal_home_url": portal_home_url(request_host_from_headers(request.headers)),
            "write_authenticated": _has_write_session(request),
        },
    )


@app.post("/books/{book_id}/delete")
def delete_saved_book(
    request: Request,
    book_id: int,
):
    _require_write_session(request)

    if not delete_book(book_id):
        raise HTTPException(status_code=404, detail="Book not found")

    return RedirectResponse(url="/", status_code=303)


@app.post("/books/{book_id}/progress")
def update_book_progress(
    request: Request,
    book_id: int,
    reading_status: str = Form(...),
    current_page: int = Form(default=0),
    current_chapter: str = Form(default=""),
    progress_percent: int = Form(default=0),
    redirect_to: str = Form(default=""),
):
    _require_write_session(request)
    if not get_book(book_id):
        raise HTTPException(status_code=404, detail="Book not found")

    update_progress(
        book_id=book_id,
        reading_status=reading_status,
        current_page=current_page,
        current_chapter=current_chapter,
        progress_percent=progress_percent,
    )

    return RedirectResponse(url=_safe_redirect(redirect_to) or f"/books/{book_id}", status_code=303)


@app.post("/books/{book_id}/chapters")
def create_book_chapter(
    request: Request,
    book_id: int,
    title: str = Form(...),
):
    _require_write_session(request)
    if not get_book(book_id):
        raise HTTPException(status_code=404, detail="Book not found")

    try:
        create_chapter(book_id=book_id, title=title)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@app.post("/books/{book_id}/chapters/bulk")
def create_book_chapters_bulk(
    request: Request,
    book_id: int,
    titles: list[str] = Form(default=[]),
):
    _require_write_session(request)
    if not get_book(book_id):
        raise HTTPException(status_code=404, detail="Book not found")

    try:
        create_chapters(book_id=book_id, titles=titles)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@app.post("/books/{book_id}/chapter-statuses")
def update_book_chapter_statuses(
    request: Request,
    book_id: int,
    done_chapter_ids: list[int] = Form(default=[]),
    redirect_to: str = Form(default=""),
):
    _require_write_session(request)
    if not get_book(book_id):
        raise HTTPException(status_code=404, detail="Book not found")

    update_chapter_statuses(book_id=book_id, done_chapter_ids=done_chapter_ids)

    return RedirectResponse(url=_safe_redirect(redirect_to) or f"/books/{book_id}", status_code=303)


@app.get("/books/{book_id}/toc-candidates")
def get_book_toc_candidates(book_id: int):
    book = get_book(book_id)

    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    return fetch_toc_candidates(book)


@app.post("/chapters/{chapter_id}")
def update_book_chapter(
    request: Request,
    chapter_id: int,
    is_done: str = Form(default="0"),
    comment: str = Form(default=""),
    redirect_to: str = Form(default=""),
):
    _require_write_session(request)
    book_id = update_chapter(
        chapter_id=chapter_id,
        is_done=is_done == "1",
        comment=comment,
    )

    if not book_id:
        raise HTTPException(status_code=404, detail="Chapter not found")

    return RedirectResponse(url=_safe_redirect(redirect_to) or f"/books/{book_id}", status_code=303)


@app.post("/chapters/{chapter_id}/comment")
def update_book_chapter_comment(
    request: Request,
    chapter_id: int,
    comment: str = Form(default=""),
    redirect_to: str = Form(default=""),
):
    _require_write_session(request)
    book_id = update_chapter_comment(chapter_id=chapter_id, comment=comment)

    if not book_id:
        raise HTTPException(status_code=404, detail="Chapter not found")

    return RedirectResponse(url=_safe_redirect(redirect_to) or f"/books/{book_id}", status_code=303)


@app.post("/chapters/{chapter_id}/delete")
def delete_book_chapter(
    request: Request,
    chapter_id: int,
):
    _require_write_session(request)

    book_id = delete_chapter(chapter_id)

    if not book_id:
        raise HTTPException(status_code=404, detail="Chapter not found")

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@app.post("/books/{book_id}/memos")
def create_book_memo(
    request: Request,
    book_id: int,
    chapter_id: int = Form(default=0),
    memo_title: str = Form(default=""),
    content: str = Form(...),
    page: int = Form(default=0),
):
    _require_write_session(request)
    if not get_book(book_id):
        raise HTTPException(status_code=404, detail="Book not found")

    try:
        create_memo(
            book_id=book_id,
            chapter_id=chapter_id or None,
            title=memo_title,
            content=content,
            page=page,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@app.post("/memos/{memo_id}/delete")
def delete_book_memo(
    request: Request,
    memo_id: int,
):
    _require_write_session(request)

    book_id = delete_memo(memo_id)

    if not book_id:
        raise HTTPException(status_code=404, detail="Memo not found")

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@app.get("/health")
def health():
    return {
        "service": "book-memo",
        "status": "ok",
    }


@app.get("/api/search")
def search_api(q: str = "", limit: int = 5):
    limit = max(1, min(limit, 20))
    return {
        "results": search_books_and_memos(q, limit=limit),
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
        if _record_auth_failure(client):
            return _write_login_response(request, "비밀번호 실패가 반복되어 잠시 후 다시 시도해주세요.", 429, safe_next_path)
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
        if _record_auth_failure(client):
            raise HTTPException(status_code=429, detail="비밀번호 실패가 반복되어 잠시 후 다시 시도해주세요.")
        raise HTTPException(status_code=403, detail="삭제 비밀번호가 올바르지 않습니다.")
    _clear_auth_failures(client)


def _require_write_session(request: Request) -> None:
    if not _has_write_session(request):
        if "text/html" in request.headers.get("accept", ""):
            login_path = f"/auth/login?next_path={quote(_browser_return_path(request), safe='')}"
            raise HTTPException(status_code=303, headers={"Location": login_path})
        raise HTTPException(status_code=401, detail="메모 쓰기 인증이 필요합니다.")


def _browser_return_path(request: Request) -> str:
    referer = urlsplit(request.headers.get("referer", ""))
    expected_origin = urlsplit(_request_origin(request))
    if (referer.scheme, referer.netloc) != (expected_origin.scheme, expected_origin.netloc):
        return "/"

    path = referer.path or "/"
    if referer.query:
        path = f"{path}?{referer.query}"
    return _safe_redirect(path) or "/"


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
            "title": "책 메모 쓰기 로그인",
            "error": error,
            "next_path": next_path,
        },
        status_code=status_code,
    )


def _auth_rate_limited(client: str) -> bool:
    with _auth_rate_limit_lock():
        _reload_auth_failures()
        if _prune_expired_auth_failures():
            _persist_auth_failures()
        return len(_AUTH_FAILURES.get(client, [])) >= AUTH_RATE_LIMIT_MAX_FAILURES


def _record_auth_failure(client: str) -> bool:
    """Record a failed attempt and report whether the client was already limited."""
    with _auth_rate_limit_lock():
        _reload_auth_failures()
        _prune_expired_auth_failures()
        failures = _AUTH_FAILURES.get(client, [])
        if len(failures) >= AUTH_RATE_LIMIT_MAX_FAILURES:
            return True
        failures.append(datetime.now(timezone.utc))
        _AUTH_FAILURES[client] = failures
        _persist_auth_failures()
        return False


def _clear_auth_failures(client: str) -> None:
    with _auth_rate_limit_lock():
        _reload_auth_failures()
        changed = _prune_expired_auth_failures()
        if _AUTH_FAILURES.pop(client, None) is not None:
            changed = True
        if changed:
            _persist_auth_failures()


@contextmanager
def _auth_rate_limit_lock():
    lock_path = AUTH_RATE_LIMIT_STATE_PATH.with_name(f".{AUTH_RATE_LIMIT_STATE_PATH.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _reload_auth_failures() -> None:
    _AUTH_FAILURES.clear()
    try:
        raw_state = json.loads(AUTH_RATE_LIMIT_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return

    if not isinstance(raw_state, dict):
        return
    for client, timestamps in raw_state.items():
        if not isinstance(client, str) or not isinstance(timestamps, list):
            continue
        failures = [_parse_auth_failure_timestamp(timestamp) for timestamp in timestamps]
        active_failures = [failure for failure in failures if failure is not None]
        if active_failures:
            _AUTH_FAILURES[client] = active_failures


def _parse_auth_failure_timestamp(timestamp: object) -> datetime | None:
    if not isinstance(timestamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _prune_expired_auth_failures() -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=AUTH_RATE_LIMIT_WINDOW_SECONDS)
    active = {
        client: [failed_at for failed_at in failures if failed_at >= cutoff]
        for client, failures in _AUTH_FAILURES.items()
    }
    active = {client: failures for client, failures in active.items() if failures}
    if active != _AUTH_FAILURES:
        _AUTH_FAILURES.clear()
        _AUTH_FAILURES.update(active)
        return True
    return False


def _persist_auth_failures() -> None:
    state = {
        client: [failed_at.isoformat() for failed_at in failures]
        for client, failures in _AUTH_FAILURES.items()
    }
    temporary_path = AUTH_RATE_LIMIT_STATE_PATH.with_name(
        f".{AUTH_RATE_LIMIT_STATE_PATH.name}.{secrets.token_hex(8)}.tmp"
    )
    try:
        AUTH_RATE_LIMIT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with temporary_path.open("w", encoding="utf-8") as state_file:
            json.dump(state, state_file, ensure_ascii=False, separators=(",", ":"))
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_path, AUTH_RATE_LIMIT_STATE_PATH)
    finally:
        temporary_path.unlink(missing_ok=True)


def _client_id(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded_for:
        return forwarded_for
    return request.client.host if request.client else "unknown"


def _safe_redirect(redirect_to: str) -> str:
    if redirect_to.startswith("/") and not redirect_to.startswith("//"):
        return redirect_to

    return ""
