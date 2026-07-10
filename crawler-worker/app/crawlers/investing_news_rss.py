from __future__ import annotations

import re

from app.crawlers.rss_news import build_google_news_rss_url, search_rss_news


INVESTING_SOURCE = "Investing.com 한국어"


def search_investing_news_rss(limit: int = 50) -> list[dict]:
    articles = search_rss_news(
        feed_urls=[build_google_news_rss_url("site:kr.investing.com/news", freshness="")],
        category="INVESTING",
        source_name=INVESTING_SOURCE,
        provider_name="Google News RSS",
        limit=max(limit, 8),
        source_filter=INVESTING_SOURCE,
    )
    cleaned_articles: list[dict] = []
    for article in articles:
        if article.get("source") != INVESTING_SOURCE:
            continue
        title = _clean_title(article.get("title", ""))
        if not title or _is_malformed_title(title):
            continue
        cleaned = dict(article)
        cleaned["title"] = title
        cleaned["title_ko"] = title
        cleaned["title_original"] = title
        cleaned_articles.append(cleaned)
    return cleaned_articles[:limit]


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
