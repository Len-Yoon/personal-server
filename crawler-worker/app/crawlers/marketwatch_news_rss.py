from __future__ import annotations

from app.crawlers.rss_news import search_rss_news


MARKETWATCH_RSS_FEEDS = [
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://feeds.marketwatch.com/marketwatch/markets/",
    "https://feeds.marketwatch.com/marketwatch/investing/",
]


def search_marketwatch_news_rss(
    category: str,
    limit: int = 20,
    source_filter: str = "",
) -> list[dict]:
    return search_rss_news(
        feed_urls=MARKETWATCH_RSS_FEEDS,
        category=category,
        source_name="MarketWatch",
        provider_name="MarketWatch RSS",
        limit=limit,
        source_filter=source_filter,
    )
