from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
from urllib.error import URLError
from urllib.request import Request, urlopen
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo


REQUEST_TIMEOUT_SECONDS = 8
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def search_rss_news(
    feed_urls: list[str],
    category: str,
    source_name: str,
    provider_name: str,
    limit: int = 20,
    source_filter: str = "",
    today_only: bool = False,
) -> list[dict]:
    try:
        import feedparser
    except ImportError:
        return []

    articles: list[dict] = []

    for feed_url in feed_urls:
        try:
            request = Request(feed_url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml,application/xml,text/xml,*/*"})
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                feed = feedparser.parse(response)
        except (OSError, TimeoutError, URLError, ValueError, Exception):
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
            if today_only and not _is_today(published):
                continue
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
                    "summary": _html_to_text(summary),
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
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return ""

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc).isoformat()


def _is_today(value: str, today=None) -> bool:
    parsed_value = _parse_published_datetime(value)
    if parsed_value is None:
        return False

    korea_date = parsed_value.astimezone(ZoneInfo("Asia/Seoul")).date()
    if today is None:
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    return korea_date == today


def _parse_published_datetime(value: str) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


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


def _html_to_text(value: str) -> str:
    value = value or ""

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return _strip_html_with_fallback(value)

    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return _cleanup_text(text)


def _strip_html_with_fallback(value: str) -> str:
    class _TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts: list[str] = []

        def handle_data(self, data: str) -> None:
            if data:
                self.parts.append(data)

    parser = _TextExtractor()
    parser.feed(value)
    return _cleanup_text(" ".join(parser.parts))


def _cleanup_text(value: str) -> str:
    return " ".join(unescape(value).split())
