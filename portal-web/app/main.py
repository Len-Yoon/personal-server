from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routers import dashboard

app = FastAPI(title="Personal Server Portal")

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(dashboard.router)


@app.get("/health")
def health():
    return {
        "service": "portal-web",
        "status": "ok"
    }