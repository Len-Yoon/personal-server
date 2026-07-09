from __future__ import annotations

from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus


def search_rss_news(
    feed_urls: list[str],
    category: str,
    source_name: str,
    provider_name: str,
    limit: int = 20,
    source_filter: str = "",
) -> list[dict]:
    try:
        import feedparser
    except ImportError:
        return []

    articles: list[dict] = []

    for feed_url in feed_urls:
        try:
            feed = feedparser.parse(feed_url)
        except Exception:
            continue

        for entry in getattr(feed, "entries", []):
            title = getattr(entry, "title", "")
            link = getattr(entry, "link", "")

            if not title or not link:
                continue

            published = (
                getattr(entry, "published", "")
                or getattr(entry, "updated", "")
                or getattr(entry, "pubDate", "")
            )
            summary = (
                getattr(entry, "summary", "")
                or getattr(entry, "description", "")
                or ""
            )
            entry_source = source_name
            if hasattr(entry, "source") and hasattr(entry.source, "title"):
                entry_source = entry.source.title

            articles.append(
                {
                    "category": category,
                    "title": title,
                    "title_ko": title,
                    "title_original": title,
                    "url": link,
                    "source": entry_source,
                    "published_at": published,
                    "published_at_sort": _parse_published_at(published),
                    "summary": summary,
                    "provider": provider_name,
                }
            )

        if len(articles) >= limit:
            break

    if source_filter:
        normalized_filter = source_filter.casefold()
        articles = [
            article
            for article in articles
            if normalized_filter in article.get("source", "").casefold()
        ]

    return _dedupe_articles(articles)[:limit]


def build_google_news_rss_url(query: str, freshness: str = "1d") -> str:
    query_with_freshness = f"({query}) when:{freshness}" if freshness else query
    encoded_query = quote_plus(query_with_freshness)
    return (
        "https://news.google.com/rss/search?"
        f"q={encoded_query}"
        "&hl=ko"
        "&gl=KR"
        "&ceid=KR:ko"
    )


def _parse_published_at(value: str) -> str:
    if not value:
        return ""

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return ""

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc).isoformat()


def _dedupe_articles(articles: list[dict]) -> list[dict]:
    seen = set()
    deduped = []

    for article in articles:
        url = article.get("url")
        if not url or url in seen:
            continue

        seen.add(url)
        deduped.append(article)

    return deduped
