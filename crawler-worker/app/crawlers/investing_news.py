from __future__ import annotations

from app.crawlers.investing_news_browser import search_investing_news_browser
from app.crawlers.investing_news_html import search_investing_news_html
from app.crawlers.news_quality import filter_high_quality_articles


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

    filtered_articles = filter_high_quality_articles(
        articles,
        category=category,
        limit=limit,
    )

    if len(filtered_articles) >= limit:
        return filtered_articles[:limit]

    fallback_articles = search_investing_news_html(
        category=category,
        limit=limit,
        source_filter=source_filter,
    )

    combined_articles = filtered_articles + fallback_articles
    return filter_high_quality_articles(
        combined_articles,
        category=category,
        limit=limit,
    )
