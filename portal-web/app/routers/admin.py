import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Body, Form, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.admin_status import build_admin_status_context, format_status_checked_at
from app.services.homeops import ALLOWED_SERVICES, get_homeops_service
from app.services.host_urls import portal_home_url
from app.services.security import (
    append_security_event,
    append_user_event,
    auth_rate_limited,
    clear_auth_failures,
    create_auth_session,
    has_auth_session,
    is_production_environment,
    record_auth_failure,
    security_status,
)
from app.services.system_status import get_dashboard_status, get_service_health

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")


@router.get("/admin/security")
def admin_security_status(request: Request, x_security_password: str = Header(default="")):
    _require_security_password(request, x_security_password)
    append_security_event("security_dashboard_viewed")
    return _disable_cache(security_status())


@router.get("/admin/status")
def admin_status_login(request: Request):
    if has_auth_session("homeops_admin", request.cookies.get("homeops_admin_session", "")):
        return _render_authenticated_admin_status(request)
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

    return _render_authenticated_admin_status(request, issue_homeops_session=True)


def _render_authenticated_admin_status(request: Request, issue_homeops_session: bool = False):
    append_security_event("admin_status_viewed")
    system_status = get_dashboard_status()
    context = build_admin_status_context(
        system_status=system_status,
        service_health=get_service_health(),
        security=security_status(),
    )
    context["homeops_summary"] = get_homeops_service().latest_summary()
    response = templates.TemplateResponse(
        "admin_status.html",
        {
            "request": request,
            "title": "관리자 상태",
            "authenticated": True,
            "error": "",
            "status_checked_at": format_status_checked_at(
                str((system_status.get("host") or {}).get("captured_at") or "")
            ),
            "portal_home_url": portal_home_url(_request_host(request)),
            **context,
        },
    )
    if issue_homeops_session:
        response.set_cookie(
            "homeops_admin_session",
            create_auth_session("homeops_admin", 900),
            max_age=900,
            httponly=True,
            samesite="lax",
            secure=is_production_environment(),
        )
    return _disable_cache(response)


@router.post("/admin/homeops/diagnose")
def homeops_diagnose(request: Request, password: str = Form(default=""), x_homeops_password: str = Header(default="")):
    _require_homeops_authorization(request, password or x_homeops_password)
    get_homeops_service().diagnose_all()
    return RedirectResponse(url="/admin/status", status_code=303)


@router.post("/admin/homeops/restart-all")
def homeops_restart_all(request: Request, password: str = Form(default=""), x_homeops_password: str = Header(default="")):
    _require_homeops_authorization(request, password or x_homeops_password)
    get_homeops_service().restart_all()
    return RedirectResponse(url="/admin/status", status_code=303)


@router.post("/admin/homeops/{incident_id}/approve")
def homeops_approve(incident_id: str, request: Request, password: str = Form(default=""), x_homeops_password: str = Header(default="")):
    _require_homeops_authorization(request, password or x_homeops_password)
    get_homeops_service().approve_incident(incident_id, _client_id(request))
    return RedirectResponse(url="/admin/status", status_code=303)


@router.post("/admin/homeops/{incident_id}/execute")
def homeops_execute(incident_id: str, request: Request, password: str = Form(default=""), x_homeops_password: str = Header(default="")):
    _require_homeops_authorization(request, password or x_homeops_password)
    get_homeops_service().execute_approved_incident(incident_id)
    return RedirectResponse(url="/admin/status", status_code=303)


@router.post("/internal/homeops/scan")
def homeops_scheduled_scan(x_homeops_scheduler_secret: str = Header(default="")):
    configured = os.getenv("HOMEOPS_SCHEDULER_SECRET", "")
    if not configured or not secrets.compare_digest(x_homeops_scheduler_secret, configured):
        raise HTTPException(status_code=403, detail="scheduler_access_denied")
    homeops = get_homeops_service()
    host = get_dashboard_status().get("host") or {}
    homeops.observe_host_memory(host.get("memory_percent"))
    results = []
    for service in sorted(ALLOWED_SERVICES):
        try:
            results.append(homeops.create_diagnosis(service, record_healthy=False))
        except OSError:
            append_security_event("homeops_scheduled_diagnosis_unavailable", service=service)
    return {"status": "ok", "diagnosed": len(results)}


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
        os.getenv("ADMIN_STATUS_PASSWORD", "").strip()
        or os.getenv("FILE_MANAGER_PASSWORD", "").strip()
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
        if record_auth_failure("security_dashboard", client):
            append_security_event("security_dashboard_rate_limited", client=client)
            raise HTTPException(status_code=429, detail="관리자 인증 실패가 반복되어 잠시 후 다시 시도해주세요.")
        append_security_event("security_dashboard_auth_failed")
        raise HTTPException(status_code=401, detail="관리자 비밀번호가 올바르지 않습니다.")
    clear_auth_failures("security_dashboard", client)


def _require_homeops_authorization(request: Request, password: str) -> None:
    if has_auth_session("homeops_admin", request.cookies.get("homeops_admin_session", "")):
        return
    _require_security_password(request, password)


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


def _disable_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response
