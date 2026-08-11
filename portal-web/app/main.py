from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.routers import dashboard, files, portfolio
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


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


@app.middleware("http")
async def reject_cross_origin_unsafe_requests(request: Request, call_next):
    if request.method in _SAFE_METHODS or request.headers.get("origin") == _request_origin(request):
        return await call_next(request)

    response = JSONResponse(
        status_code=403,
        content={"detail": "Cross-origin requests are not allowed."},
    )
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response

app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

app.include_router(dashboard.router)
app.include_router(files.router)
app.include_router(portfolio.router)


@app.get("/health")
def health():
    return {
        "service": "portal-web",
        "status": "ok"
    }
