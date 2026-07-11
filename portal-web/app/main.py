from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routers import dashboard, files
from app.services.security import SECURITY_HEADERS

app = FastAPI(title="Personal Server Portal")
APP_DIR = Path(__file__).resolve().parent


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response

app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

app.include_router(dashboard.router)
app.include_router(files.router)


@app.get("/health")
def health():
    return {
        "service": "portal-web",
        "status": "ok"
    }
