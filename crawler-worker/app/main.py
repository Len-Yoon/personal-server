from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
import hmac
import os
from fastapi.responses import JSONResponse
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from app.routers import news
from app.services.news_scheduler import InvestingNewsScheduler
from app.services.news_collection_metrics import render_metrics
from app.services.news_collection_status import NewsCollectionStatusStore


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
_PUBLIC_ORIGIN = "https://news.len.pe.kr"
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://img.youtube.com https://image.aladin.co.kr "
        "https://books.google.com https://covers.openlibrary.org; "
        "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'; frame-src 'self' https://www.youtube.com"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def _request_origin(request: Request) -> str:
    if request.url.hostname == "news.len.pe.kr":
        return _PUBLIC_ORIGIN
    return str(request.base_url).rstrip("/")


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


@app.get("/internal/metrics", response_class=PlainTextResponse)
def internal_metrics(request: Request):
    configured_token = os.getenv("NEWS_METRICS_BEARER_TOKEN", "")
    authorization = request.headers.get("authorization", "")
    expected = f"Bearer {configured_token}"
    if not configured_token or not hmac.compare_digest(authorization, expected):
        return PlainTextResponse("not found\n", status_code=404)
    return Response(
        content=render_metrics(
            NewsCollectionStatusStore().snapshot(),
            refresh_interval_seconds=int(os.getenv("NEWS_REFRESH_INTERVAL_SECONDS", "300")),
        ),
        headers={"content-type": "text/plain; version=0.0.4"},
    )


@app.get("/health")
def health():
    return {
        "service": "crawler-worker",
        "status": "ok",
    }
