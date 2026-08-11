from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.routers import news
from app.services.news_scheduler import InvestingNewsScheduler


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler = InvestingNewsScheduler()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()


app = FastAPI(title="Global Market News Hub", lifespan=lifespan)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'self'; object-src 'none'; "
        "frame-ancestors 'none'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
        "frame-src https://www.youtube.com https://www.youtube-nocookie.com; "
        "connect-src 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


def _request_origin(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    scheme = forwarded_proto or request.url.scheme
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    host = forwarded_host or request.headers.get("host", "")
    return f"{scheme}://{host}"


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

app.include_router(news.router)


@app.get("/health")
def health():
    return {
        "service": "crawler-worker",
        "status": "ok",
    }
