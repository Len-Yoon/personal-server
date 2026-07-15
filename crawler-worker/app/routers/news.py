from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.templating import Jinja2Templates

from app.services.datetime_format import format_news_datetime
from app.services.host_urls import portal_home_url, request_host_from_headers
from app.services.news_archive import (
    collect_korean_news,
    collect_market_news,
    get_categories,
    get_korean_categories,
    list_recent_news,
)


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["news_datetime"] = format_news_datetime


def _portal_home_url(request: Request) -> str:
    return portal_home_url(request_host_from_headers(request.headers))


def _category_limit(category: str) -> int:
    return 50 if category.upper() == "INVESTING" else 24


def _korean_category_limit(category: str) -> int:
    return 24


templates.env.globals["portal_home_url"] = _portal_home_url


@router.get("/")
def home(request: Request):
    market_categories = [
        category
        for category in get_categories()
        if category["code"] != "INVESTING"
    ]

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "title": "글로벌 뉴스 허브",
            "categories": market_categories,
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
    category: str = Query(default="INVESTING"),
    refresh: bool = Query(default=False),
):
    result = collect_market_news(
        category=category,
        limit=_category_limit(category),
        force_refresh=refresh,
    )

    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "title": result["label"],
            "result": result,
            "categories": get_categories(),
            "category_path": "/category",
            "refresh_url": f"/category?category={result['category']}&refresh=true",
        },
    )


@router.get("/news")
def korean_news_home(request: Request):
    return templates.TemplateResponse(
        "news_home.html",
        {
            "request": request,
            "title": "한국어 뉴스 허브",
            "categories": get_korean_categories(),
        },
    )


@router.get("/news/category")
def korean_category_page(
    request: Request,
    category: str = Query(default="KR_WORLD"),
    refresh: bool = Query(default=False),
):
    result = collect_korean_news(
        category=category,
        limit=_korean_category_limit(category),
        force_refresh=refresh,
    )

    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "title": result["label"],
            "result": result,
            "categories": get_korean_categories(),
            "category_path": "/news/category",
            "refresh_url": f"/news/category?category={result['category']}&refresh=true",
        },
    )


@router.get("/api/category")
def category_api(
    category: str = Query(default="INVESTING"),
    refresh: bool = Query(default=False),
):
    return collect_market_news(
        category=category,
        limit=_category_limit(category),
        force_refresh=refresh,
    )


@router.get("/news/api/category")
def korean_category_api(
    category: str = Query(default="KR_WORLD"),
    refresh: bool = Query(default=False),
):
    return collect_korean_news(
        category=category,
        limit=_korean_category_limit(category),
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
