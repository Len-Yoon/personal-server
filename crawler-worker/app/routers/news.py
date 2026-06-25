from fastapi import APIRouter, Query, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.services.news_service import collect_market_news, get_categories
from app.services.openai_summary_service import summarize_market_news


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


class ArticleSummaryRequest(BaseModel):
    category: str
    title: str
    url: str
    source: str = ""
    published_at: str = ""
    provider: str = ""


@router.get("/")
def home(request: Request):
    categories = get_categories()

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "title": "글로벌 뉴스 허브",
            "categories": categories,
        },
    )


@router.get("/category")
def category_page(
    request: Request,
    category: str = Query(default="WORLD"),
    refresh: bool = Query(default=False),
):
    result = collect_market_news(
        category=category,
        limit=24,
        force_refresh=refresh,
    )

    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "title": result["label"],
            "result": result,
            "categories": get_categories(),
        },
    )


@router.get("/api/category")
def category_api(
    category: str = Query(default="WORLD"),
    refresh: bool = Query(default=False),
):
    return collect_market_news(
        category=category,
        limit=24,
        force_refresh=refresh,
    )


@router.post("/api/summarize")
def summarize_article(payload: ArticleSummaryRequest):
    article = payload.model_dump()
    return summarize_market_news(article)