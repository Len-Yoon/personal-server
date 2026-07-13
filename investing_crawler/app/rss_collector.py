from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


INVESTING_SOURCE = "Investing.com 한국어"
OFFICIAL_INVESTING_RSS_URL = "https://kr.investing.com/rss/news.rss"
OFFICIAL_INVESTING_RSS_URLS = (
    "https://kr.investing.com/rss/news.rss",
    "https://kr.investing.com/rss/news_25.rss",
    "https://kr.investing.com/rss/news_1.rss",
    "https://kr.investing.com/rss/news_11.rss",
)
GOOGLE_NEWS_BASE = "https://news.google.com/rss/search?"
REQUEST_TIMEOUT_SECONDS = 20
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"


def build_investing_google_news_rss_url(freshness: str = "") -> str:
    return ",".join(OFFICIAL_INVESTING_RSS_URLS)


def build_google_fallback_rss_url() -> str:
    query = quote_plus("site:kr.investing.com/news when:1d")
    return f"{GOOGLE_NEWS_BASE}q={query}&hl=ko&gl=KR&ceid=KR:ko"


def collect_investing_news(limit: int = 50, feed_url: str = "") -> list[dict[str, str]]:
    try:
        import feedparser
    except ImportError:
        return []

    articles: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    feed_urls = [
        value.strip()
        for value in (feed_url or build_investing_google_news_rss_url()).split(",")
        if value.strip()
    ]
    for current_feed_url in feed_urls:
        request = Request(
            current_feed_url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml,application/xml,text/xml"},
        )
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            feed = feedparser.parse(response)

        for entry in getattr(feed, "entries", []):
            source = _source_title(entry)
            title = _clean_title(getattr(entry, "title", ""))
            url = getattr(entry, "link", "")
            direct_feed_without_source = (
                not source and current_feed_url.startswith("https://kr.investing.com/rss/")
            )
            if (
                source not in {INVESTING_SOURCE, "Investing.com"}
                and not direct_feed_without_source
            ) or not title or _is_malformed_title(title) or not url:
                continue
            if url in seen_urls:
                continue

            published_at = _parse_published_at(
                getattr(entry, "published", "") or getattr(entry, "updated", "")
            )
            if not _is_today(published_at):
                continue
            articles.append(
                {
                    "title": title,
                    "url": url,
                    "published_label": _format_published_label(published_at),
                    "published_at": published_at,
                    "source": source or INVESTING_SOURCE,
                }
            )
            seen_urls.add(url)
            if len(articles) >= limit:
                return articles
    if any(current_url in OFFICIAL_INVESTING_RSS_URLS for current_url in feed_urls):
        fallback_articles = collect_investing_news(
            limit=limit,
            feed_url=build_google_fallback_rss_url(),
        )
        return _merge_articles(articles, fallback_articles, limit)

    return articles


def _is_today(value: str) -> bool:
    if not value:
        return False
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ZoneInfo("Asia/Seoul")).date() == datetime.now(
        ZoneInfo("Asia/Seoul")
    ).date()


def _merge_articles(
    primary: list[dict[str, str]],
    fallback: list[dict[str, str]],
    limit: int,
) -> list[dict[str, str]]:
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    merged: list[dict[str, str]] = []
    for article in [*primary, *fallback]:
        url = str(article.get("url", "")).strip()
        title = str(article.get("title", "")).strip().casefold()
        if not url or url in seen_urls or title in seen_titles:
            continue
        seen_urls.add(url)
        seen_titles.add(title)
        merged.append(article)
        if len(merged) >= limit:
            break
    return merged


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
