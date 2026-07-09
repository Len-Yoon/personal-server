from __future__ import annotations

from app.crawlers.rss_news import build_google_news_rss_url, search_rss_news


GOOGLE_NEWS_QUERIES = {
    "WORLD": "world markets OR global markets OR inflation OR rates",
    "NASDAQ": "nasdaq OR semiconductors OR artificial intelligence OR tech stocks",
    "GOLD": "gold prices OR gold futures OR dollar OR inflation",
    "HK50": "hang seng OR hong kong stocks OR china markets",
}


def search_google_news_rss(
    category: str,
    limit: int = 20,
    source_filter: str = "",
) -> list[dict]:
    category = category.upper()
    query = GOOGLE_NEWS_QUERIES.get(category, GOOGLE_NEWS_QUERIES["WORLD"])
    feed_url = build_google_news_rss_url(query, freshness="1d")

    return search_rss_news(
        feed_urls=[feed_url],
        category=category,
        source_name="Google News",
        provider_name="Google News RSS",
        limit=limit,
        source_filter=source_filter,
    )
