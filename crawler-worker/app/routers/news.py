import os
import secrets

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.services.news_service import collect_market_news, get_categories
from app.services.openai_summary_service import summarize_market_news
from app.services.saved_news_service import (
    delete_saved_news,
    get_saved_news_by_url,
    save_news_summary,
    search_saved_news,
)


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


class ArticleSummaryRequest(BaseModel):
    category: str
    title: str
    url: str
    title_ko: str = ""
    title_original: str = ""
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


@router.get("/saved")
def saved_news_page(
    request: Request,
    q: str = Query(default=""),
):
    saved_news = search_saved_news(query=q)

    return templates.TemplateResponse(
        "saved.html",
        {
            "request": request,
            "title": "저장한 뉴스",
            "saved_news": saved_news,
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
    saved_news = search_saved_news(query=q, limit=limit)
    return {
        "results": [
            {
                "title": item.get("title_ko") or item.get("title", "저장 뉴스"),
                "description": item.get("summary", {}).get("brief") or item.get("source", ""),
                "url": item.get("url", "#"),
            }
            for item in saved_news
        ]
    }


@router.post("/api/summarize")
def summarize_article(payload: ArticleSummaryRequest):
    article = payload.model_dump()
    saved_news = get_saved_news_by_url(article.get("url", ""))

    if saved_news:
        return {
            "ok": True,
            "cached": True,
            "model": saved_news.get("model", ""),
            "article": article,
            "summary": saved_news.get("summary", {}),
            "save": {
                "saved": True,
                "id": saved_news.get("id"),
                "created_at": saved_news.get("created_at"),
                "updated_at": saved_news.get("updated_at"),
            },
        }

    result = summarize_market_news(article)

    if result.get("ok"):
        result["save"] = save_news_summary(result)

    return result


@router.delete("/api/saved/{saved_news_id}")
def delete_saved_news_api(
    saved_news_id: int,
    x_delete_password: str = Header(default=""),
):
    _require_delete_password(x_delete_password)

    deleted = delete_saved_news(saved_news_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Saved news not found")

    return {
        "ok": True,
        "deleted": True,
        "id": saved_news_id,
    }


def _require_delete_password(password: str) -> None:
    configured_password = os.getenv("DELETE_PASSWORD", "").strip()

    if not configured_password:
        raise HTTPException(status_code=403, detail="삭제 비밀번호가 설정되지 않았습니다.")

    if not secrets.compare_digest(password, configured_password):
        raise HTTPException(status_code=403, detail="삭제 비밀번호가 올바르지 않습니다.")
