from contextlib import asynccontextmanager

from fastapi import FastAPI
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

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(news.router)


@app.get("/health")
def health():
    return {
        "service": "crawler-worker",
        "status": "ok",
    }
