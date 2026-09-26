import hmac
import os
from collections import deque
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from app.routers import admin, dashboard, files, portfolio
from app.services.http_metrics import HttpMetrics
from app.services.security import SECURITY_HEADERS

app = FastAPI(title="Personal Server Portal")
APP_DIR = Path(__file__).resolve().parent
PORTFOLIO_ALLOWED_PATHS = {"/", "/admin", "/admin/login", "/admin/save"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_PUBLIC_ORIGINS = {
    "len.pe.kr": "https://len.pe.kr",
    "portfolio.len.pe.kr": "https://portfolio.len.pe.kr",
    "file.len.pe.kr": "https://file.len.pe.kr",
    "admin.len.pe.kr": "https://admin.len.pe.kr",
}
_METRICS_PATH = "/internal/metrics"
_HTTP_METRICS = HttpMetrics()


def _request_origin(request: Request) -> str:
    return _PUBLIC_ORIGINS.get(request.url.hostname or "", str(request.base_url).rstrip("/"))


@app.middleware("http")
async def restrict_portfolio_host(request: Request, call_next):
    if (
        portfolio.is_portfolio_host(request)
        and request.url.path not in PORTFOLIO_ALLOWED_PATHS
        and not request.url.path.startswith("/static/")
    ):
        return PlainTextResponse("Not Found", status_code=404)
    return await call_next(request)


class UploadBodyLimitMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        if path == "/files/upload":
            limit = files.file_store.MAX_UPLOAD_BYTES + files.file_store.CHUNK_SIZE
        elif path == "/files/uploads":
            limit = files.file_store.MAX_UPLOAD_TOTAL_BYTES + files.file_store.CHUNK_SIZE
        else:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", ()))
        content_length = headers.get(b"content-length", b"")
        if content_length.isdigit() and int(content_length) > limit:
            await self._reject(scope, receive, send)
            return

        received = 0
        buffered = deque()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    await self._reject(scope, receive, send)
                    return
                buffered.append(message)
                if not message.get("more_body", False):
                    break

        async def replay_receive():
            if buffered:
                return buffered.popleft()
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(status_code=413, content={"detail": "업로드 요청이 너무 큽니다."})
        await response(scope, receive, send)


app.add_middleware(UploadBodyLimitMiddleware)


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


@app.middleware("http")
async def reject_cross_origin_unsafe_requests(request: Request, call_next):
    scheduler_without_origin = (
        request.method == "POST"
        and request.url.path == "/internal/homeops/scan"
        and "origin" not in request.headers
        and admin.homeops_scheduler_secret_valid(request.headers.get("x-homeops-scheduler-secret", ""))
    )
    if request.method in _SAFE_METHODS or request.headers.get("origin") == _request_origin(request) or scheduler_without_origin:
        return await call_next(request)

    response = JSONResponse(
        status_code=403,
        content={"detail": "Cross-origin requests are not allowed."},
    )
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


@app.middleware("http")
async def record_http_metrics(request: Request, call_next):
    started_at = perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        _record_http_metric(request, status_code=500, started_at=started_at)
        raise
    _record_http_metric(request, status_code=response.status_code, started_at=started_at)
    return response


def _record_http_metric(request: Request, *, status_code: int, started_at: float) -> None:
    if request.url.path != _METRICS_PATH:
        route = request.scope.get("route")
        route_template = getattr(route, "path", None) or "unmatched"
        _HTTP_METRICS.record(
            method=request.method,
            route=route_template,
            status_code=status_code,
            duration_seconds=perf_counter() - started_at,
        )

app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

app.include_router(dashboard.router)
app.include_router(admin.router)
app.include_router(files.router)
app.include_router(portfolio.router)


@app.get(_METRICS_PATH, response_class=PlainTextResponse)
def internal_metrics(request: Request):
    configured_token = os.getenv("PORTAL_METRICS_BEARER_TOKEN", "")
    authorization = request.headers.get("authorization", "")
    if not configured_token or not hmac.compare_digest(authorization, f"Bearer {configured_token}"):
        return PlainTextResponse("Not Found\n", status_code=404)
    return Response(
        content=_HTTP_METRICS.render(),
        headers={"content-type": "text/plain; version=0.0.4"},
    )


@app.get("/health")
def health():
    return {
        "service": "portal-web",
        "status": "ok"
    }
