from __future__ import annotations

import re
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


INVESTING_SOURCE = "Investing.com 한국어"
GOOGLE_NEWS_BASE = "https://news.google.com/rss/search?"
REQUEST_TIMEOUT_SECONDS = 20
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"


def build_investing_google_news_rss_url(freshness: str = "") -> str:
    query = "site:kr.investing.com/news"
    if freshness:
        query = f"({query}) when:{freshness}"
    return (
        f"{GOOGLE_NEWS_BASE}q={quote_plus(query)}"
        "&hl=ko&gl=KR&ceid=KR:ko"
    )


def collect_investing_news(limit: int = 50, feed_url: str = "") -> list[dict[str, str]]:
    try:
        import feedparser
    except ImportError:
        return []

    request = Request(
        feed_url or build_investing_google_news_rss_url(),
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml,application/xml,text/xml"},
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        feed = feedparser.parse(response)

    articles: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for entry in getattr(feed, "entries", []):
        source = _source_title(entry)
        title = _clean_title(getattr(entry, "title", ""))
        url = getattr(entry, "link", "")
        if source != INVESTING_SOURCE or not title or _is_malformed_title(title) or not url:
            continue
        if url in seen_urls:
            continue

        published_at = _parse_published_at(
            getattr(entry, "published", "") or getattr(entry, "updated", "")
        )
        articles.append(
            {
                "title": title,
                "url": url,
                "published_label": _format_published_label(published_at),
                "published_at": published_at,
                "source": source,
            }
        )
        seen_urls.add(url)
        if len(articles) >= limit:
            break
    return articles


def _source_title(entry) -> str:
    source = getattr(entry, "source", {}) or {}
    return str(source.get("title", "")).strip()


def _clean_title(value: str) -> str:
    title = " ".join((value or "").split())
    title = re.sub(r"\s+-\s+Investing\.com 한국어$", "", title)
    return re.sub(r"\s+By\s+Investing\.com$", "", title, flags=re.IGNORECASE).strip()


def _is_malformed_title(title: str) -> bool:
    return title.casefold() in {"by investing.com", "by investing.com 한국어"}


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


def _format_published_label(value: str) -> str:
    if not value:
        return "확인 불가"
    from datetime import datetime

    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST")
