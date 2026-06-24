from fastapi import APIRouter, Query, Request
from fastapi.templating import Jinja2Templates

from app.services.news_service import collect_market_news, get_categories

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


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
):
    result = collect_market_news(category=category, limit=24)

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
):
    return collect_market_news(category=category, limit=24)