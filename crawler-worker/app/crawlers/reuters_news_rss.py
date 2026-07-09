from __future__ import annotations

from app.crawlers.rss_news import search_rss_news


REUTERS_RSS_FEEDS = {
    "WORLD": [
        "https://feeds.reuters.com/reuters/topNews",
        "https://feeds.reuters.com/reuters/worldNews",
    ],
    "NASDAQ": [
        "https://feeds.reuters.com/reuters/technologyNews",
        "https://feeds.reuters.com/reuters/businessNews",
    ],
    "GOLD": [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.reuters.com/reuters/marketsNews",
    ],
    "HK50": [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.reuters.com/reuters/marketsNews",
    ],
}


def search_reuters_news_rss(
    category: str,
    limit: int = 20,
    source_filter: str = "",
) -> list[dict]:
    return search_rss_news(
        feed_urls=REUTERS_RSS_FEEDS.get(category.upper(), REUTERS_RSS_FEEDS["WORLD"]),
        category=category,
        source_name="Reuters",
        provider_name="Reuters RSS",
        limit=limit,
        source_filter=source_filter,
    )
