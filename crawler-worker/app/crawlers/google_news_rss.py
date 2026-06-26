from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser


def search_google_news_rss(
    query: str,
    category: str,
    limit: int = 20,
    freshness: str = "1d",
):
    query_with_freshness = f"({query}) when:{freshness}" if freshness else query
    encoded_query = quote_plus(query_with_freshness)

    rss_url = (
        "https://news.google.com/rss/search?"
        f"q={encoded_query}"
        "&hl=ko"
        "&gl=KR"
        "&ceid=KR:ko"
    )

    feed = feedparser.parse(rss_url)

    articles = []

    for entry in feed.entries:
        title = getattr(entry, "title", "")
        link = getattr(entry, "link", "")
        published = getattr(entry, "published", "")
        published_at_sort = _parse_published_at(published)
        source = "Google News RSS"

        if hasattr(entry, "source") and hasattr(entry.source, "title"):
            source = entry.source.title

        if not title or not link:
            continue

        articles.append(
            {
                "category": category,
                "title": title,
                "title_ko": title,
                "title_original": title,
                "url": link,
                "source": source,
                "published_at": published,
                "published_at_sort": published_at_sort,
                "summary": "",
                "provider": "Google News RSS KR",
            }
        )

    articles.sort(
        key=lambda article: article.get("published_at_sort") or "",
        reverse=True,
    )

    return articles[:limit]


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
