import os
import secrets
import ipaddress

from fastapi import APIRouter, Body, Form, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.admin_status import build_admin_status_context, format_status_checked_at
from app.services.global_search import search_all
from app.services.host_urls import portal_home_url, service_base_urls, service_url
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


@router.get("/")
def dashboard(request: Request, q: str = ""):
    host = _request_host(request)
    local_mode = _is_local_host(host)
    base_urls = service_base_urls(host)
    if host == _configured_host("FILES_HOSTNAME") or host.startswith("file."):
        return RedirectResponse(url="/files", status_code=302)
    if host == _configured_host("ADMIN_HOSTNAME") or host.startswith("admin."):
        return RedirectResponse(url="/admin/status", status_code=302)

    services = [
        {
            "icon": "N",
            "name": "뉴스 허브",
            "description": "일반 뉴스와 주식 뉴스를 수집하고, 최근 보관 뉴스와 원문을 빠르게 확인합니다.",
            "url": "/news",
            "status": "운영중",
            "meta": "News / Stock / Archive",
        },
        {
            "icon": "Y",
            "name": "유튜브 메모장",
            "description": "유튜브 영상별 학습 메모와 타임스탬프를 기록합니다.",
            "url": "/memo",
            "status": "운영중",
            "meta": "YouTube / Memo / Timestamp",
        },
        {
            "icon": "B",
            "name": "책 메모장",
            "description": "읽은 책을 저장하고 목차별 진행률과 독서 메모를 관리합니다.",
            "url": "/books",
            "status": "운영중",
            "meta": "Book / Reading / Memo",
        },
        {
            "icon": "F",
            "name": "파일함",
            "description": "개인 서버에 파일을 올리고 내려받는 가벼운 웹 파일 관리자입니다.",
            "url": "/files",
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
            "search_results": search_all(
                q,
                public_base_urls=base_urls,
                local_base_urls=base_urls,
                prefer_local=local_mode,
            )
            if q.strip()
            else None,
            "portal_home_url": portal_home_url(host),
        },
    )


@router.get("/news")
def news_entry(request: Request):
    host = _request_host(request)
    return RedirectResponse(url=service_url("NEWS_SERVICE_URL", host, os.getenv("NEWS_SERVICE_URL", "")), status_code=302)


@router.get("/memo")
def memo_entry(request: Request):
    host = _request_host(request)
    return RedirectResponse(url=service_url("YOUTUBE_MEMO_URL", host, os.getenv("YOUTUBE_MEMO_URL", "")), status_code=302)


@router.get("/books")
def books_entry(request: Request):
    host = _request_host(request)
    return RedirectResponse(url=service_url("BOOK_MEMO_URL", host, os.getenv("BOOK_MEMO_URL", "")), status_code=302)


@router.get("/admin/security")
def admin_security_status(request: Request, x_security_password: str = Header(default="")):
    _require_security_password(request, x_security_password)
    append_security_event("security_dashboard_viewed")
    return _disable_cache(security_status())


@router.get("/admin/status")
def admin_status_login(request: Request):
    host = _request_host(request)
    response = templates.TemplateResponse(
        "admin_status.html",
        {
            "request": request,
            "title": "관리자 상태",
            "authenticated": False,
            "error": "",
            "portal_home_url": portal_home_url(host),
        },
    )
    return _disable_cache(response)


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
        response = templates.TemplateResponse(
            "admin_status.html",
            {
                "request": request,
                "title": "관리자 상태",
                "authenticated": False,
                "error": message,
                "portal_home_url": portal_home_url(_request_host(request)),
            },
            status_code=exc.status_code,
        )
        return _disable_cache(response)

    append_security_event("admin_status_viewed")
    system_status = get_dashboard_status()
    context = build_admin_status_context(
        system_status=system_status,
        service_health=get_service_health(),
        security=security_status(),
    )
    response = templates.TemplateResponse(
        "admin_status.html",
        {
            "request": request,
            "title": "관리자 상태",
            "authenticated": True,
            "error": "",
            "status_checked_at": format_status_checked_at(system_status.get("captured_at", "")),
            "portal_home_url": portal_home_url(_request_host(request)),
            **context,
        },
    )
    return _disable_cache(response)


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


def _request_host(request: Request) -> str:
    forwarded_host = request.headers.get("x-forwarded-host", "").strip()
    if forwarded_host:
        return forwarded_host.split(",")[0].strip().lower()

    host = request.headers.get("host", "").strip()
    if host:
        return host.split(":")[0].strip().lower()

    return ""


def _configured_host(env_name: str) -> str:
    return os.getenv(env_name, "").strip().lower()


def _is_local_host(host: str) -> bool:
    host = host.split(":")[0]
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"}:
        return True

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False

    return ip.is_private or ip.is_loopback or ip.is_link_local


def _disable_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response
