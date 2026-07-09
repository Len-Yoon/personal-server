from __future__ import annotations

from concurrent.futures import as_completed, ThreadPoolExecutor

from app.crawlers.ap_news_rss import search_ap_news_rss
from app.crawlers.google_news_rss import search_google_news_rss
from app.crawlers.investing_news import search_investing_news
from app.crawlers.marketwatch_news_rss import search_marketwatch_news_rss
from app.crawlers.reuters_news_rss import search_reuters_news_rss


SOURCE_LIMIT_RATIO = {
    "WORLD": ("google", "reuters", "marketwatch", "ap", "investing"),
    "NASDAQ": ("google", "reuters", "marketwatch", "ap", "investing"),
    "GOLD": ("google", "reuters", "marketwatch", "ap", "investing"),
    "HK50": ("google", "reuters", "ap", "marketwatch", "investing"),
}


def collect_news_from_sources(category: str, limit: int = 24) -> list[dict]:
    category = category.upper()
    source_order = SOURCE_LIMIT_RATIO.get(category, SOURCE_LIMIT_RATIO["WORLD"])
    collected: list[dict] = []
    per_source_limit = max(limit, 10)

    executor = ThreadPoolExecutor(max_workers=len(source_order))
    futures = {
        executor.submit(_collect_from_source, source_name, category, per_source_limit): source_name
        for source_name in source_order
    }

    try:
        for future in as_completed(futures):
            collected.extend(future.result())

            if futures[future] != "investing" and len(_dedupe_articles(collected)) >= limit:
                return _dedupe_articles(collected)[:limit]
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return _dedupe_articles(collected)[:limit]


def _collect_from_source(source_name: str, category: str, limit: int) -> list[dict]:
    try:
        if source_name == "google":
            return search_google_news_rss(category=category, limit=limit)
        if source_name == "reuters":
            return search_reuters_news_rss(category=category, limit=limit)
        if source_name == "investing":
            return search_investing_news(category=category, limit=limit)
        if source_name == "ap":
            return search_ap_news_rss(category=category, limit=limit)
        if source_name == "marketwatch":
            return search_marketwatch_news_rss(category=category, limit=limit)
    except Exception:
        return []

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
