from __future__ import annotations

import json
import os
import tempfile
from threading import Lock, Thread
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.crawlers.rss_news import _html_to_text
from app.services.news_sources import collect_news_from_sources


PROJECT_DATA_ROOT = Path(__file__).resolve().parents[3] / "data"
ARCHIVE_PATH = Path(
    os.getenv(
        "NEWS_ARCHIVE_PATH",
        PROJECT_DATA_ROOT / "crawler-worker" / "news_archive.json",
    )
)
CACHE_TTL_SECONDS = int(os.getenv("NEWS_REFRESH_INTERVAL_SECONDS", "3600"))
RETENTION_DAYS = int(os.getenv("NEWS_RETENTION_DAYS", "7"))

_ARCHIVE_WRITE_LOCK = Lock()
_REFRESH_LOCK = Lock()
_REFRESHING_CATEGORIES: set[str] = set()


def collect_market_news(
    category: str,
    limit: int = 24,
    force_refresh: bool = False,
) -> dict[str, Any]:
    category = _normalize_category(category)
    now = _now()
    archive = _load_archive()
    archive, purged = _purge_archive(archive, now)
    if purged:
        archive["updated_at"] = _iso(now)
        _save_archive(archive)

    category_articles = _get_category_articles(archive["articles"], category)
    latest_collected_at = _latest_collected_at(category_articles)

    if (
        category_articles
        and not force_refresh
        and latest_collected_at
        and (now - latest_collected_at).total_seconds() < CACHE_TTL_SECONDS
    ):
        return _build_result(
            category=category,
            articles=category_articles,
            limit=limit,
            cached=True,
            age_seconds=int((now - latest_collected_at).total_seconds()),
        )

    if category_articles and not force_refresh:
        _schedule_refresh(category, limit)
        return _build_result(
            category=category,
            articles=category_articles,
            limit=limit,
            cached=True,
            age_seconds=int((now - latest_collected_at).total_seconds())
            if latest_collected_at
            else 0,
        )

    try:
        fresh_articles = collect_news_from_sources(
            category=category,
            limit=limit,
        )
    except Exception:
        fresh_articles = category_articles
    stored_articles = [
        _attach_archive_metadata(article, category=category, now=now)
        for article in fresh_articles
    ]

    archive["articles"] = _merge_articles(archive["articles"], stored_articles)
    archive["updated_at"] = _iso(now)
    _save_archive(archive)

    category_articles = _get_category_articles(archive["articles"], category)

    return _build_result(
        category=category,
        articles=category_articles,
        limit=limit,
        cached=False,
        age_seconds=0,
    )


def list_recent_news(query: str = "", limit: int = 50) -> list[dict[str, Any]]:
    archive = _load_archive()
    archive, purged = _purge_archive(archive, _now())
    if purged:
        archive["updated_at"] = _iso(_now())
        _save_archive(archive)
    articles = _dedupe_by_url(archive["articles"])

    if query.strip():
        keyword = query.strip().casefold()
        articles = [
            article
            for article in articles
            if _matches_query(article, keyword)
        ]

    articles.sort(key=_sort_key, reverse=True)
    return articles[:limit]


def get_categories() -> list[dict[str, str]]:
    return [
        {
            "code": code,
            "label": details["label"],
            "description": details["description"],
        }
        for code, details in _category_map().items()
    ]


def _build_result(
    category: str,
    articles: list[dict[str, Any]],
    limit: int,
    cached: bool,
    age_seconds: int,
) -> dict[str, Any]:
    sorted_articles = sorted(
        articles,
        key=_sort_key,
        reverse=True,
    )

    return {
        "category": category,
        "label": _category_label(category),
        "description": _category_description(category),
        "count": len(sorted_articles[:limit]),
        "articles": sorted_articles[:limit],
        "cache": {
            "hit": cached,
            "age_seconds": age_seconds,
            "ttl_seconds": CACHE_TTL_SECONDS,
        },
    }


def _load_archive() -> dict[str, Any]:
    if not ARCHIVE_PATH.exists():
        return {"updated_at": "", "articles": []}

    try:
        with ARCHIVE_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"updated_at": "", "articles": []}

    articles = data.get("articles", [])
    if not isinstance(articles, list):
        articles = []

    normalized_articles = []
    changed = False

    for article in articles:
        if not isinstance(article, dict):
            continue

        normalized = _sanitize_article(article)
        if normalized != article:
            changed = True
        normalized_articles.append(normalized)

    archive = {
        "updated_at": str(data.get("updated_at", "")),
        "articles": normalized_articles,
    }

    if changed:
        try:
            _save_archive(archive)
        except OSError:
            pass

    return archive


def _save_archive(archive: dict[str, Any]) -> None:
    with _ARCHIVE_WRITE_LOCK:
        ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(ARCHIVE_PATH.parent),
            prefix=f".{ARCHIVE_PATH.name}.",
            suffix=".tmp",
        ) as handle:
            json.dump(archive, handle, ensure_ascii=False, indent=2)
            temp_path = Path(handle.name)

        temp_path.replace(ARCHIVE_PATH)


def _schedule_refresh(category: str, limit: int) -> None:
    with _REFRESH_LOCK:
        if category in _REFRESHING_CATEGORIES:
            return
        _REFRESHING_CATEGORIES.add(category)

    def _run() -> None:
        try:
            _refresh_category(category=category, limit=limit)
        finally:
            with _REFRESH_LOCK:
                _REFRESHING_CATEGORIES.discard(category)

    Thread(target=_run, daemon=True).start()


def _refresh_category(category: str, limit: int) -> None:
    now = _now()
    archive = _load_archive()
    archive, purged = _purge_archive(archive, now)
    if purged:
        archive["updated_at"] = _iso(now)

    try:
        fresh_articles = collect_news_from_sources(
            category=category,
            limit=limit,
        )
    except Exception:
        fresh_articles = []
    stored_articles = [
        _attach_archive_metadata(article, category=category, now=now)
        for article in fresh_articles
    ]

    archive["articles"] = _merge_articles(archive["articles"], stored_articles)
    archive["updated_at"] = _iso(now)
    _save_archive(archive)


def _purge_archive(archive: dict[str, Any], now: datetime) -> tuple[dict[str, Any], bool]:
    retention = timedelta(days=RETENTION_DAYS)
    kept_articles: list[dict[str, Any]] = []
    changed = False

    for article in archive.get("articles", []):
        expires_at = _parse_dt(str(article.get("expires_at", "")))
        collected_at = _parse_dt(str(article.get("collected_at", "")))

        if expires_at and expires_at >= now:
            kept_articles.append(article)
            continue

        if collected_at and now - collected_at <= retention:
            article["expires_at"] = _iso(collected_at + retention)
            kept_articles.append(article)
            changed = True
            continue

        changed = True

    archive["articles"] = kept_articles
    return archive, changed


def _merge_articles(
    existing_articles: list[dict[str, Any]],
    new_articles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged_by_url: dict[str, dict[str, Any]] = {}

    for article in existing_articles + new_articles:
        url = str(article.get("url", "")).strip()
        if not url:
            continue

        previous = merged_by_url.get(url)
        if previous is None:
            merged_by_url[url] = article
            continue

        previous_collected_at = _parse_dt(str(previous.get("collected_at", "")))
        current_collected_at = _parse_dt(str(article.get("collected_at", "")))
        if current_collected_at and (
            not previous_collected_at or current_collected_at >= previous_collected_at
        ):
            merged_by_url[url] = article

    merged = list(merged_by_url.values())
    merged.sort(key=_sort_key, reverse=True)
    return merged


def _dedupe_by_url(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []

    for article in sorted(
        articles,
        key=_sort_key,
        reverse=True,
    ):
        url = str(article.get("url", "")).strip()
        if not url or url in seen:
            continue

        seen.add(url)
        deduped.append(article)

    return deduped


def _get_category_articles(
    articles: list[dict[str, Any]],
    category: str,
) -> list[dict[str, Any]]:
    category_articles = [
        article
        for article in articles
        if str(article.get("category", "")).upper() == category
    ]
    return _dedupe_by_url(category_articles)


def _attach_archive_metadata(
    article: dict[str, Any],
    category: str,
    now: datetime,
) -> dict[str, Any]:
    stored = _sanitize_article(article)
    stored["category"] = category
    stored["collected_at"] = _iso(now)
    stored["expires_at"] = _iso(now + timedelta(days=RETENTION_DAYS))
    return stored


def _sanitize_article(article: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(article)

    for field in ("title", "title_ko", "title_original", "summary", "source", "provider"):
        value = sanitized.get(field, "")
        if isinstance(value, str):
            sanitized[field] = _html_to_text(value)

    return sanitized


def _latest_collected_at(articles: list[dict[str, Any]]) -> datetime | None:
    parsed = [
        _parse_dt(str(article.get("collected_at", "")))
        for article in articles
    ]
    parsed = [item for item in parsed if item is not None]
    if not parsed:
        return None
    return max(parsed)


def _sort_key(article: dict[str, Any]) -> tuple[str, str]:
    published_at_sort = str(article.get("published_at_sort", "")).strip()
    collected_at = str(article.get("collected_at", "")).strip()
    return (published_at_sort or collected_at or "", str(article.get("title", "")))


def _matches_query(article: dict[str, Any], keyword: str) -> bool:
    haystack = " ".join(
        str(article.get(field, ""))
        for field in ("title", "title_ko", "title_original", "source", "category", "summary")
    ).casefold()
    return keyword in haystack


def _normalize_category(category: str) -> str:
    category = (category or "WORLD").upper()
    return category if category in _category_map() else "WORLD"


def _category_label(category: str) -> str:
    return _category_map()[category]["label"]


def _category_description(category: str) -> str:
    return _category_map()[category]["description"]


def _category_map() -> dict[str, dict[str, str]]:
    return {
        "WORLD": {
            "label": "세계 뉴스",
            "description": "전쟁, 금리, 달러, 원자재, 주요국 경제 이슈",
        },
        "NASDAQ": {
            "label": "나스닥 선물",
            "description": "NASDAQ, 미국 기술주, 반도체, AI, 미국 금리 이슈",
        },
        "GOLD": {
            "label": "금 선물",
            "description": "Gold futures, 달러, 금리, 안전자산, 인플레이션",
        },
        "HK50": {
            "label": "홍콩50",
            "description": "Hang Seng, Hong Kong 50, 중국 증시, 중국 경기 이슈",
        },
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
