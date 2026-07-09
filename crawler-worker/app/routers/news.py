from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.templating import Jinja2Templates

from app.services.host_urls import portal_home_url, request_host_from_headers
from app.services.news_archive import list_recent_news
from app.services.news_service import collect_market_news, get_categories


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _portal_home_url(request: Request) -> str:
    return portal_home_url(request_host_from_headers(request.headers))


templates.env.globals["portal_home_url"] = _portal_home_url


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


@router.get("/saved")
def recent_news_page(
    request: Request,
    q: str = Query(default=""),
):
    recent_news = list_recent_news(query=q)

    return templates.TemplateResponse(
        "saved.html",
        {
            "request": request,
            "title": "보관 뉴스",
            "recent_news": recent_news,
            "query": q,
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


@router.get("/api/search")
def search_api(q: str = Query(default=""), limit: int = Query(default=5, ge=1, le=20)):
    recent_news = list_recent_news(query=q, limit=limit)
    return {
        "results": [
            {
                "title": item.get("title_ko") or item.get("title", "뉴스"),
                "description": item.get("summary") or item.get("source", ""),
                "snippet": " · ".join(
                    part
                    for part in [
                        item.get("category", ""),
                        item.get("source", ""),
                        item.get("published_at", ""),
                    ]
                    if part
                ),
                "meta": " · ".join(
                    part
                    for part in [item.get("category", ""), item.get("collected_at", "")]
                    if part
                ),
                "url": item.get("url", "#"),
            }
            for item in recent_news
        ]
    }
