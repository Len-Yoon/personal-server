from __future__ import annotations

from app.crawlers.investing_news_browser import search_investing_news_browser
from app.crawlers.investing_news_html import search_investing_news_html


def search_investing_news(
    category: str,
    limit: int = 20,
    source_filter: str = "Investing.com",
) -> list[dict]:
    articles = search_investing_news_browser(
        category=category,
        limit=limit,
        source_filter=source_filter,
    )

    if articles:
        return articles

    return search_investing_news_html(
        category=category,
        limit=limit,
        source_filter=source_filter,
    )
