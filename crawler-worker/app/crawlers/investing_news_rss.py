from __future__ import annotations

import re

from app.crawlers.rss_news import search_rss_news


INVESTING_SOURCE = "Investing.com 한국어"
INVESTING_FEED_URLS = [
    "https://kr.investing.com/rss/news.rss",
    "https://kr.investing.com/rss/news_25.rss",
    "https://kr.investing.com/rss/news_1.rss",
    "https://kr.investing.com/rss/news_11.rss",
]
TARGET_TOPIC_KEYWORDS = (
    "나스닥",
    "nasdaq",
    "일본",
    "닛케이",
    "니케이",
    "nikkei",
    "topix",
    "엔화",
    "원유",
    "유가",
    "wti",
    "브렌트",
    "brent",
    "opec",
    "금값",
    "골드",
    "gold",
    "xau",
)


def search_investing_news_rss(limit: int = 50) -> list[dict]:
    articles = search_rss_news(
        feed_urls=INVESTING_FEED_URLS,
        category="INVESTING",
        source_name=INVESTING_SOURCE,
        provider_name="Investing.com RSS",
        limit=max(limit, 8),
        source_filter=INVESTING_SOURCE,
    )
    cleaned_articles: list[dict] = []
    for article in articles:
        if article.get("source") != INVESTING_SOURCE:
            continue
        title = _clean_title(article.get("title", ""))
        if not title or _is_malformed_title(title) or not _is_target_topic(title):
            continue
        cleaned = dict(article)
        cleaned["title"] = title
        cleaned["title_ko"] = title
        cleaned["title_original"] = title
        cleaned_articles.append(cleaned)
    return cleaned_articles[:limit]


def _is_target_topic(title: str) -> bool:
    normalized = title.casefold()
    return any(keyword in normalized for keyword in TARGET_TOPIC_KEYWORDS)


def _clean_title(value: str) -> str:
    title = " ".join((value or "").split())
    title = re.sub(r"\s+-\s+Investing\.com 한국어$", "", title)
    title = re.sub(r"\s+By\s+Investing\.com$", "", title, flags=re.IGNORECASE)
    return title.strip()


def _is_malformed_title(value: str) -> bool:
    return _clean_title(value).casefold() in {
        "by investing.com",
        "by investing.com 한국어",
        "",
    }
