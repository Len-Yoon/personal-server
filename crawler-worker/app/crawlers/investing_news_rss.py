from __future__ import annotations

import re
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from app.crawlers.rss_news import search_rss_news


INVESTING_SOURCE = "Investing.com 한국어"
INVESTING_FEED_URLS = [
    "https://kr.investing.com/rss/news.rss",
]
TOPIC_PATTERNS = {
    "금": re.compile(r"금\s*(?:값|가격|선물|시세|시장)|골드|gold|xau", re.IGNORECASE),
    "원유": re.compile(
        r"원유|유가|국제유가|wti|브렌트|brent|opec|crude(?:\s+oil)?|oil prices?",
        re.IGNORECASE,
    ),
    "일본": re.compile(r"일본|닛케이|도쿄|엔화|boj|topix", re.IGNORECASE),
}


def search_investing_news_rss(limit: int = 50) -> list[dict]:
    direct_articles = search_rss_news(
        feed_urls=INVESTING_FEED_URLS,
        category="INVESTING",
        source_name=INVESTING_SOURCE,
        provider_name="Investing.com RSS",
        limit=max(limit, 8),
        source_filter=INVESTING_SOURCE,
    )
    cleaned_articles = _clean_articles(direct_articles)
    return _dedupe_articles(cleaned_articles)[:limit]


def _clean_articles(articles: list[dict]) -> list[dict]:
    cleaned_articles: list[dict] = []
    for article in articles:
        if article.get("source") != INVESTING_SOURCE:
            continue
        title = _clean_title(article.get("title", ""))
        if not title or _is_malformed_title(title) or not _is_today(article.get("published_at", "")):
            continue
        cleaned = dict(article)
        cleaned["title"] = title
        cleaned["title_ko"] = title
        cleaned["title_original"] = title
        cleaned["topics"] = _classify_topics(
            title,
            str(cleaned.get("summary", "")),
        )
        cleaned_articles.append(cleaned)
    return cleaned_articles


def _classify_topics(title: str, summary: str) -> list[str]:
    searchable_text = f"{title} {summary}".strip()
    topics = [
        topic
        for topic, pattern in TOPIC_PATTERNS.items()
        if pattern.search(searchable_text)
    ]
    return topics or ["세계동향"]


def _dedupe_articles(articles: list[dict]) -> list[dict]:
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    deduped: list[dict] = []
    for article in articles:
        url = str(article.get("url", "")).strip()
        title = str(article.get("title", "")).strip().casefold()
        if not url or url in seen_urls or title in seen_titles:
            continue
        seen_urls.add(url)
        seen_titles.add(title)
        deduped.append(article)
    return deduped


def _is_today(value: str, today: date | None = None) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError, OverflowError):
            return False

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    korea_date = parsed.astimezone(ZoneInfo("Asia/Seoul")).date()
    return korea_date == (today or datetime.now(ZoneInfo("Asia/Seoul")).date())


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
