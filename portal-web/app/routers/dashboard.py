import os
import ipaddress
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.global_search import search_all
from app.services.host_urls import portal_home_url, service_base_urls, service_url
from app.routers.portfolio import is_portfolio_host, render_public_portfolio

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")


@router.get("/")
def dashboard(request: Request, q: str = ""):
    host = _request_host(request)
    if is_portfolio_host(request):
        return render_public_portfolio(request)
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
            "body_class": "atlas-body",
            "main_class": "atlas-main",
            "current_year": datetime.now().year,
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
