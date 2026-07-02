import os
import secrets

from fastapi import APIRouter, Body, Form, Header, HTTPException, Request
from fastapi.templating import Jinja2Templates

from app.services.admin_status import build_admin_status_context
from app.services.global_search import search_all
from app.services.security import (
    append_security_event,
    append_user_event,
    auth_rate_limited,
    clear_auth_failures,
    record_auth_failure,
    security_status,
)
from app.services.system_status import get_dashboard_status, get_service_health

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

DEFAULT_SERVICE_URLS = {
    "NEWS_SERVICE_URL": "http://news.lenserver.com",
    "YOUTUBE_MEMO_URL": "http://memo.lenserver.com",
    "BOOK_MEMO_URL": "http://book.lenserver.com",
}


def _service_url(env_name: str) -> str:
    configured_url = os.getenv(env_name, "").strip()
    if not configured_url:
        return DEFAULT_SERVICE_URLS[env_name]

    local_hosts = (
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "host.docker.internal",
    )
    if any(host in configured_url for host in local_hosts):
        return DEFAULT_SERVICE_URLS[env_name]

    return configured_url


@router.get("/")
def dashboard(request: Request, q: str = ""):
    services = [
        {
            "icon": "N",
            "name": "뉴스 허브",
            "description": "일반 뉴스와 주식 뉴스를 수집하고, 나중에 AI 요약까지 연결합니다.",
            "url": _service_url("NEWS_SERVICE_URL"),
            "status": "운영중",
            "meta": "News / Stock / Summary",
        },
        {
            "icon": "Y",
            "name": "유튜브 메모장",
            "description": "유튜브 영상별 학습 메모와 타임스탬프를 기록합니다.",
            "url": _service_url("YOUTUBE_MEMO_URL"),
            "status": "운영중",
            "meta": "YouTube / Memo / Timestamp",
        },
        {
            "icon": "B",
            "name": "책 메모장",
            "description": "읽은 책을 저장하고 목차별 진행률과 독서 메모를 관리합니다.",
            "url": _service_url("BOOK_MEMO_URL"),
            "status": "운영중",
            "meta": "Book / Reading / Memo",
        },
        {
            "icon": "F",
            "name": "파일함",
            "description": "개인 서버에 파일을 올리고 내려받는 가벼운 웹 파일 관리자입니다.",
            "url": "http://lenserver.com/files",
            "status": "운영중",
            "meta": "Files / Upload / Download",
        },
        {
            "icon": "A",
            "name": "관리자 상태",
            "description": "비밀번호 인증 후 서버 상태와 보안 상태를 한 화면에서 확인합니다.",
            "url": "/admin/status",
            "status": "운영중",
            "meta": "Admin / Server / Security",
        },
        {
            "icon": "T",
            "name": "자동매매 결과지",
            "description": "매매 결과, 수익률, 전략별 복기 내용을 관리합니다.",
            "url": "#",
            "status": "나중에",
            "meta": "Trading / Report / Review",
        },
    ]

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "title": "Len의 개인서버",
            "services": services,
            "demo_mode": os.getenv("DEMO_MODE", "").lower() in {"1", "true", "yes", "on"},
            "query": q.strip(),
            "search_results": search_all(q) if q.strip() else None,
        },
    )


@router.get("/admin/security")
def admin_security_status(request: Request, x_security_password: str = Header(default="")):
    _require_security_password(request, x_security_password)
    append_security_event("security_dashboard_viewed")
    return security_status()


@router.get("/admin/status")
def admin_status_login(request: Request):
    return templates.TemplateResponse(
        "admin_status.html",
        {
            "request": request,
            "title": "관리자 상태",
            "authenticated": False,
            "error": "",
        },
    )


@router.post("/admin/status")
def admin_status_page(request: Request, password: str = Form(default="")):
    try:
        _require_security_password(request, password)
    except HTTPException as exc:
        message = "관리자 비밀번호가 올바르지 않습니다."
        if exc.status_code == 429:
            message = "인증 실패가 반복되어 잠시 후 다시 시도해주세요."
        elif exc.status_code == 403:
            message = "관리자 비밀번호가 설정되지 않았습니다."
        return templates.TemplateResponse(
            "admin_status.html",
            {
                "request": request,
                "title": "관리자 상태",
                "authenticated": False,
                "error": message,
            },
            status_code=exc.status_code,
        )

    append_security_event("admin_status_viewed")
    context = build_admin_status_context(
        system_status=get_dashboard_status(),
        service_health=get_service_health(),
        security=security_status(),
    )
    return templates.TemplateResponse(
        "admin_status.html",
        {
            "request": request,
            "title": "관리자 상태",
            "authenticated": True,
            "error": "",
            **context,
        },
    )


@router.post("/admin/events")
async def admin_user_event(request: Request, payload: dict = Body(default_factory=dict)):
    event = str(payload.get("event", ""))
    append_user_event(
        event,
        path=str(payload.get("path", "")),
        target=str(payload.get("target", "")),
        href=str(payload.get("href", "")),
        query=str(payload.get("query", "")),
        client=_client_id(request),
    )
    return {"ok": True}


def _require_security_password(request: Request, password: str) -> None:
    configured_password = (
        os.getenv("FILE_MANAGER_PASSWORD", "").strip()
        or os.getenv("DELETE_PASSWORD", "").strip()
    )
    client = _client_id(request)

    if auth_rate_limited("security_dashboard", client):
        append_security_event("security_dashboard_rate_limited", client=client)
        raise HTTPException(status_code=429, detail="관리자 인증 실패가 반복되어 잠시 후 다시 시도해주세요.")

    if not configured_password:
        append_security_event("security_dashboard_blocked", reason="password_not_configured")
        raise HTTPException(status_code=403, detail="관리자 비밀번호가 설정되지 않았습니다.")

    if not secrets.compare_digest(password, configured_password):
        record_auth_failure("security_dashboard", client)
        append_security_event("security_dashboard_auth_failed")
        raise HTTPException(status_code=401, detail="관리자 비밀번호가 올바르지 않습니다.")
    clear_auth_failures("security_dashboard", client)


def _client_id(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded_for:
        return forwarded_for
    return request.client.host if request.client else "unknown"
