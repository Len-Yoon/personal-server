from __future__ import annotations

import json
import ssl
from urllib.parse import quote
from urllib.request import Request, urlopen


VELOG_TRENDING_API = "https://cache.velcdn.com/api/trending-posts"
REQUEST_TIMEOUT_SECONDS = 20
def search_velog_trending(limit: int = 20) -> list[dict]:
    payload = _fetch_trending_posts(limit=max(limit * 3, 30))
    articles = [_to_article(post) for post in payload]
    return articles[:limit]


def _fetch_trending_posts(limit: int) -> list[dict]:
    url = f"{VELOG_TRENDING_API}?timeframe=week&limit={min(limit, 50)}&offset=0"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "personal-server-news/1.0",
        },
    )
    try:
        with urlopen(
            request,
            timeout=REQUEST_TIMEOUT_SECONDS,
            context=_ssl_context(),
        ) as response:
            payload = json.load(response)
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _to_article(post: dict) -> dict:
    user = post.get("user") or {}
    username = str(user.get("username") or "velog")
    slug = str(post.get("urlSlug") or post.get("id") or "post")
    url = f"https://velog.io/@{quote(username)}/{quote(slug)}"
    title = str(post.get("title") or "벨로그 트렌딩 글")
    description = str(post.get("shortDescription") or "벨로그 주간 트렌딩 개발 글")
    likes = int(post.get("likes") or 0)
    comments = int(post.get("comments") or 0)
    published_at = str(post.get("releasedAt") or post.get("updatedAt") or "")

    return {
        "category": "KR_STACK",
        "title": title,
        "title_ko": title,
        "title_original": title,
        "url": url,
        "source": "Velog",
        "provider": "Velog Trending API",
        "published_at": published_at,
        "published_at_sort": published_at,
        "summary": f"{description} · 좋아요 {likes} · 댓글 {comments}",
        "topics": ["Velog 주간 인기글"],
        "source_status": "velog",
    }
