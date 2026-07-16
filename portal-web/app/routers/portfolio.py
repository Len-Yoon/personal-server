import hashlib
import hmac
import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.services import portfolio_store
from app.services.security import (
    append_security_event,
    auth_rate_limited,
    clear_auth_failures,
    record_auth_failure,
)


router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")
PORTFOLIO_ADMIN_COOKIE = "portfolio_admin_access"
PORTFOLIO_ADMIN_MAX_AGE = 8 * 60 * 60


@router.get("/admin")
def portfolio_admin(request: Request):
    _require_portfolio_host(request)
    if not _has_admin_access(request):
        return _disable_cache(_admin_login_response(request))

    return _disable_cache(
        templates.TemplateResponse(
            "portfolio_editor.html",
            {
                "request": request,
                "title": "포트폴리오 편집",
                "content": portfolio_store.load_portfolio_content(),
                "preview": Markup(portfolio_store.render_portfolio_markdown(portfolio_store.load_portfolio_content())),
            },
        )
    )


@router.post("/admin/login")
def portfolio_admin_login(request: Request, password: str = Form(default="")):
    _require_portfolio_host(request)
    client = _client_id(request)
    if auth_rate_limited("portfolio_admin", client):
        append_security_event("portfolio_admin_rate_limited", client=client)
        return _disable_cache(_admin_login_response(request, "인증 실패가 반복되어 잠시 후 다시 시도해주세요.", 429))

    configured_password = _admin_password()
    if not configured_password:
        append_security_event("portfolio_admin_password_missing", client=client)
        return _disable_cache(_admin_login_response(request, "관리자 비밀번호가 설정되지 않았습니다.", 403))

    if not secrets.compare_digest(password, configured_password):
        record_auth_failure("portfolio_admin", client)
        append_security_event("portfolio_admin_login_failed", client=client)
        return _disable_cache(_admin_login_response(request, "비밀번호가 올바르지 않습니다.", 403))

    clear_auth_failures("portfolio_admin", client)
    append_security_event("portfolio_admin_login_granted", client=client)
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        PORTFOLIO_ADMIN_COOKIE,
        _admin_cookie_value(configured_password),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/admin",
        max_age=PORTFOLIO_ADMIN_MAX_AGE,
    )
    return _disable_cache(response)


@router.post("/admin/save")
def save_portfolio(request: Request, content: str = Form(default="")):
    _require_portfolio_host(request)
    _require_admin_access(request)
    portfolio_store.save_portfolio_content(content)
    append_security_event("portfolio_content_saved", client=_client_id(request), length=len(content))
    return _disable_cache(RedirectResponse(url="/admin", status_code=303))


def render_public_portfolio(request: Request):
    content = portfolio_store.load_portfolio_content()
    return templates.TemplateResponse(
        "portfolio.html",
        {
            "request": request,
            "title": "포트폴리오",
            "content": Markup(portfolio_store.render_portfolio_markdown(content)),
            "has_content": bool(content.strip()),
        },
    )


def is_portfolio_host(request: Request) -> bool:
    return _request_host(request) == os.getenv("PORTFOLIO_HOSTNAME", "portfolio.len.pe.kr").strip().lower()


def _require_portfolio_host(request: Request) -> None:
    if not is_portfolio_host(request):
        raise HTTPException(status_code=404, detail="Not Found")


def _admin_password() -> str:
    return os.getenv("PORTFOLIO_ADMIN_PASSWORD", "").strip()


def _admin_cookie_value(password: str) -> str:
    return hmac.new(
        password.encode("utf-8"),
        b"personal-server-portfolio-admin",
        hashlib.sha256,
    ).hexdigest()


def _has_admin_access(request: Request) -> bool:
    password = _admin_password()
    cookie = request.cookies.get(PORTFOLIO_ADMIN_COOKIE, "")
    return bool(password and cookie) and hmac.compare_digest(cookie, _admin_cookie_value(password))


def _require_admin_access(request: Request) -> None:
    if not _has_admin_access(request):
        append_security_event("portfolio_admin_save_blocked", client=_client_id(request))
        raise HTTPException(status_code=401, detail="포트폴리오 관리자 인증이 필요합니다.")


def _admin_login_response(request: Request, error: str = "", status_code: int = 200):
    return templates.TemplateResponse(
        "portfolio_login.html",
        {"request": request, "title": "포트폴리오 관리자", "error": error},
        status_code=status_code,
    )


def _client_id(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded_for:
        return forwarded_for
    return request.client.host if request.client else "unknown"


def _request_host(request: Request) -> str:
    return request.headers.get("host", "").split(":")[0].strip().lower()


def _disable_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response
