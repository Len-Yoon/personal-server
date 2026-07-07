import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
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
from shared.host_urls import portal_home_url, request_host_from_headers

app = FastAPI(title="Youtube Memo")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
AUTH_RATE_LIMIT_MAX_FAILURES = int(os.getenv("AUTH_RATE_LIMIT_MAX_FAILURES", "5"))
AUTH_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "300"))
_AUTH_FAILURES: dict[str, list[datetime]] = {}


@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "title": "유튜브 메모장",
            "videos": list_videos(),
        },
    )


@app.post("/videos")
def create_video(url: str = Form(...)):
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
        },
    )


@app.post("/videos/{video_id}/memos")
def create_video_memo(
    video_id: int,
    memo_title: str = Form(default=""),
    content: str = Form(...),
):
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
