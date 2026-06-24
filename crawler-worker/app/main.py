from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routers import news

app = FastAPI(title="Global Market News Hub")

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(news.router)


@app.get("/health")
def health():
    return {
        "service": "crawler-worker",
        "status": "ok",
    }