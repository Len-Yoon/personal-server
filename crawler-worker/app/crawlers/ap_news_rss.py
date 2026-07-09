from __future__ import annotations

from app.crawlers.rss_news import search_rss_news


AP_RSS_FEEDS = [
    "https://apnews.com/hub/ap-top-news?output=rss",
    "https://apnews.com/hub/world-news?output=rss",
    "https://apnews.com/hub/business?output=rss",
]


def search_ap_news_rss(
    category: str,
    limit: int = 20,
    source_filter: str = "",
) -> list[dict]:
    return search_rss_news(
        feed_urls=AP_RSS_FEEDS,
        category=category,
        source_name="AP News",
        provider_name="AP News RSS",
        limit=limit,
        source_filter=source_filter,
    )
