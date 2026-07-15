from __future__ import annotations

from app.crawlers.ap_news_rss import search_ap_news_rss
from app.crawlers.google_news_rss import filter_korean_articles, search_google_news_rss
from app.crawlers.investing_news_rss import search_investing_news_rss
from app.crawlers.marketwatch_news_rss import search_marketwatch_news_rss
from app.crawlers.reuters_news_rss import search_reuters_news_rss
from app.crawlers.news_quality import filter_high_quality_articles


SOURCE_LIMIT_RATIO = {
    "INVESTING": ("investing",),
    "WORLD": ("google", "reuters", "ap", "marketwatch"),
    "NASDAQ": ("google", "reuters", "ap", "marketwatch"),
    "GOLD": ("google", "reuters", "ap", "marketwatch"),
    "HK50": ("google", "reuters", "ap", "marketwatch"),
}

KOREAN_SOURCE_LIMIT_RATIO = {
    "KR_WORLD": ("google",),
    "KR_IT": ("google",),
    "KR_AI": ("google",),
    "KR_STACK": ("google",),
}


def collect_news_from_sources(category: str, limit: int = 24) -> list[dict]:
    category = category.upper()
    if category == "INVESTING":
        return _dedupe_articles(
            _collect_from_source("investing", category, max(limit, 8))
        )[:limit]
    source_order = SOURCE_LIMIT_RATIO.get(category, SOURCE_LIMIT_RATIO["WORLD"])
    collected: list[dict] = []
    per_source_limit = max(limit, 8)

    for source_name in source_order:
        collected.extend(_collect_from_source(source_name, category, per_source_limit))

        filtered = filter_high_quality_articles(
            _dedupe_articles(collected),
            category=category,
            limit=limit,
        )
        if len(filtered) >= limit:
            return filtered[:limit]

    return filter_high_quality_articles(
        _dedupe_articles(collected),
        category=category,
        limit=limit,
    )[:limit]


def collect_korean_news_from_sources(category: str, limit: int = 24) -> list[dict]:
    category = category.upper()
    source_order = KOREAN_SOURCE_LIMIT_RATIO.get(category, KOREAN_SOURCE_LIMIT_RATIO["KR_WORLD"])
    collected: list[dict] = []
    per_source_limit = max(limit, 8)

    for source_name in source_order:
        collected.extend(_collect_korean_source(source_name, category, per_source_limit))

        filtered = filter_high_quality_articles(
            _dedupe_articles(collected),
            category=category,
            limit=limit,
        )
        if len(filtered) >= limit:
            return filtered[:limit]

    return filter_high_quality_articles(
        _dedupe_articles(collected),
        category=category,
        limit=limit,
    )[:limit]


def _collect_from_source(source_name: str, category: str, limit: int) -> list[dict]:
    try:
        if source_name == "google":
            return search_google_news_rss(category=category, limit=limit)
        if source_name == "investing":
            return search_investing_news_rss(limit=limit)
        if source_name == "reuters":
            return search_reuters_news_rss(category=category, limit=limit)
        if source_name == "ap":
            return search_ap_news_rss(category=category, limit=limit)
        if source_name == "marketwatch":
            return search_marketwatch_news_rss(category=category, limit=limit)
    except Exception:
        return []

    return []


def _collect_korean_source(source_name: str, category: str, limit: int) -> list[dict]:
    if source_name == "google":
        articles = search_google_news_rss(category=category, limit=limit)
        return filter_korean_articles(articles)
    return []


def _dedupe_articles(articles: list[dict]) -> list[dict]:
    seen: set[str] = set()
    deduped: list[dict] = []

    for article in sorted(articles, key=_sort_key, reverse=True):
        url = str(article.get("url", "")).strip()
        if not url or url in seen:
            continue

        seen.add(url)
        deduped.append(article)

    return deduped


def _sort_key(article: dict) -> tuple[str, str]:
    published = str(article.get("published_at_sort", "")).strip()
    collected_at = str(article.get("collected_at", "")).strip()
    return (published or collected_at or "", str(article.get("title", "")))
