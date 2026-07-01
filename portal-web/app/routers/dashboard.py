import os
import secrets

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.templating import Jinja2Templates

from app.services.global_search import search_all
from app.services.security import append_security_event, security_status
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
            "system_status": get_dashboard_status(),
            "service_health": get_service_health(),
            "query": q.strip(),
            "search_results": search_all(q) if q.strip() else None,
        },
    )


@router.get("/admin/security")
def admin_security_status(x_security_password: str = Header(default="")):
    _require_security_password(x_security_password)
    append_security_event("security_dashboard_viewed")
    return security_status()


def _require_security_password(password: str) -> None:
    configured_password = (
        os.getenv("FILE_MANAGER_PASSWORD", "").strip()
        or os.getenv("DELETE_PASSWORD", "").strip()
    )

    if not configured_password:
        append_security_event("security_dashboard_blocked", reason="password_not_configured")
        raise HTTPException(status_code=403, detail="관리자 비밀번호가 설정되지 않았습니다.")

    if not secrets.compare_digest(password, configured_password):
        append_security_event("security_dashboard_auth_failed")
        raise HTTPException(status_code=401, detail="관리자 비밀번호가 올바르지 않습니다.")
