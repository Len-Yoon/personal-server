from __future__ import annotations

import json
import os
import tempfile
from threading import Lock, Thread
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.crawlers.rss_news import _html_to_text
from app.services.news_sources import collect_korean_news_from_sources, collect_news_from_sources


PROJECT_DATA_ROOT = Path(__file__).resolve().parents[3] / "data"
CACHE_TTL_SECONDS = int(os.getenv("NEWS_REFRESH_INTERVAL_SECONDS", "300"))
RETENTION_DAYS = int(os.getenv("NEWS_RETENTION_DAYS", "7"))
ARCHIVE_SCHEMA_VERSION = "2026-07-15-korean-news"

_ARCHIVE_WRITE_LOCK = Lock()
_REFRESH_LOCK = Lock()
_REFRESH_WORK_LOCK = Lock()
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

    category_articles = _get_category_articles(
        archive["articles"], category, today_only=True
    )

    return _build_result(
        category=category,
        articles=category_articles,
        limit=limit,
        cached=False,
        age_seconds=0,
    )


def collect_korean_news(
    category: str,
    limit: int = 24,
    force_refresh: bool = False,
) -> dict[str, Any]:
    category = _normalize_korean_category(category)
    now = _now()
    archive = _load_archive()
    archive, purged = _purge_archive(archive, now)
    if purged:
        archive["updated_at"] = _iso(now)
        _save_archive(archive)

    category_articles = _get_category_articles(
        archive["articles"], category, today_only=True
    )
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
            label_resolver=_korean_category_label,
            description_resolver=_korean_category_description,
        )

    if category_articles and not force_refresh:
        _schedule_refresh(category, limit, korean=True)
        return _build_result(
            category=category,
            articles=category_articles,
            limit=limit,
            cached=True,
            age_seconds=int((now - latest_collected_at).total_seconds())
            if latest_collected_at
            else 0,
            label_resolver=_korean_category_label,
            description_resolver=_korean_category_description,
        )

    try:
        fresh_articles = collect_korean_news_from_sources(
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
        label_resolver=_korean_category_label,
        description_resolver=_korean_category_description,
    )


def list_recent_news(
    query: str = "",
    limit: int = 50,
    korean_only: bool = False,
    today_only: bool = False,
) -> list[dict[str, Any]]:
    archive = _load_archive()
    archive, purged = _purge_archive(archive, _now())
    if purged:
        archive["updated_at"] = _iso(_now())
        _save_archive(archive)
    articles = _dedupe_by_url(archive["articles"])
    if korean_only:
        articles = [
            article
            for article in articles
            if str(article.get("category", "")).upper().startswith("KR_")
        ]
    if today_only:
        articles = [article for article in articles if _is_today_article(article)]

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


def get_korean_categories() -> list[dict[str, str]]:
    return [
        {
            "code": code,
            "label": details["label"],
            "description": details["description"],
        }
        for code, details in _korean_category_map().items()
    ]


def _build_result(
    category: str,
    articles: list[dict[str, Any]],
    limit: int,
    cached: bool,
    age_seconds: int,
    label_resolver=None,
    description_resolver=None,
) -> dict[str, Any]:
    label_resolver = label_resolver or _category_label
    description_resolver = description_resolver or _category_description
    sorted_articles = sorted(
        articles,
        key=_sort_key,
        reverse=True,
    )
    source_status = ""
    if category == "KR_STACK":
        source_statuses = {
            str(article.get("source_status", "")).strip()
            for article in sorted_articles[:limit]
        }
        if "velog" in source_statuses:
            source_status = "velog"
        elif "reddit" in source_statuses:
            source_status = "reddit"
        else:
            source_status = "unavailable"

    return {
        "category": category,
        "label": label_resolver(category),
        "description": description_resolver(category),
        "count": len(sorted_articles[:limit]),
        "articles": sorted_articles[:limit],
        "source_status": source_status,
        "cache": {
            "hit": cached,
            "age_seconds": age_seconds,
            "ttl_seconds": CACHE_TTL_SECONDS,
        },
    }


def _load_archive() -> dict[str, Any]:
    archive_path = _archive_path()
    if not archive_path.exists():
        return {"updated_at": "", "articles": []}

    try:
        with archive_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"updated_at": "", "articles": []}

    # The production archive is outside Git and needs a one-time reset after
    # changing the news source rules.
    if (
        str(archive_path) == "/data/crawler-worker/news_archive.json"
        and data.get("schema_version") != ARCHIVE_SCHEMA_VERSION
    ):
        return {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "updated_at": "",
            "articles": [],
        }

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
        "schema_version": str(data.get("schema_version", ARCHIVE_SCHEMA_VERSION)),
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
    archive["schema_version"] = ARCHIVE_SCHEMA_VERSION
    archive_path = _archive_path()
    with _ARCHIVE_WRITE_LOCK:
        archive_path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(archive_path.parent),
            prefix=f".{archive_path.name}.",
            suffix=".tmp",
        ) as handle:
            json.dump(archive, handle, ensure_ascii=False, indent=2)
            temp_path = Path(handle.name)

        temp_path.replace(archive_path)


def _archive_path() -> Path:
    return Path(
        os.getenv(
            "NEWS_ARCHIVE_PATH",
            PROJECT_DATA_ROOT / "crawler-worker" / "news_archive.json",
        )
    )


def _schedule_refresh(category: str, limit: int, korean: bool = False) -> None:
    with _REFRESH_LOCK:
        if category in _REFRESHING_CATEGORIES or _REFRESH_WORK_LOCK.locked():
            return
        _REFRESHING_CATEGORIES.add(category)

    def _run() -> None:
        try:
            with _REFRESH_WORK_LOCK:
                _refresh_category(category=category, limit=limit, korean=korean)
        finally:
            with _REFRESH_LOCK:
                _REFRESHING_CATEGORIES.discard(category)

    Thread(target=_run, daemon=True).start()


def _refresh_category(category: str, limit: int, korean: bool = False) -> None:
    now = _now()
    archive = _load_archive()
    archive, purged = _purge_archive(archive, now)
    if purged:
        archive["updated_at"] = _iso(now)

    try:
        fresh_articles = (
            collect_korean_news_from_sources(category=category, limit=limit)
            if korean
            else collect_news_from_sources(category=category, limit=limit)
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
    today_only: bool = False,
) -> list[dict[str, Any]]:
    category_articles = [
        article
        for article in articles
        if str(article.get("category", "")).upper() == category
        and (not today_only or _is_today_article(article))
    ]
    return _dedupe_by_url(category_articles)


def _is_today_article(article: dict[str, Any]) -> bool:
    value = (
        article.get("published_at_sort")
        or article.get("published_at")
        or article.get("collected_at")
    )
    parsed = _parse_dt(str(value or ""))
    if parsed is None:
        return False
    korea_now = _now().astimezone(ZoneInfo("Asia/Seoul"))
    korea_date = parsed.astimezone(ZoneInfo("Asia/Seoul")).date()
    return korea_date == korea_now.date()


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


def _normalize_korean_category(category: str) -> str:
    category = (category or "KR_WORLD").upper()
    return category if category in _korean_category_map() else "KR_WORLD"


def _category_label(category: str) -> str:
    return _category_map()[category]["label"]


def _category_description(category: str) -> str:
    return _category_map()[category]["description"]


def _category_map() -> dict[str, dict[str, str]]:
    return {
        "INVESTING": {
            "label": "Investing.com 한국어 뉴스",
            "description": "오늘 날짜의 Investing.com 한국어 뉴스 전체",
        },
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


def _korean_category_map() -> dict[str, dict[str, str]]:
    return {
        "KR_WORLD": {
            "label": "Investing.com 뉴스",
            "description": "Investing.com 한국어 RSS에서 오늘 수집한 세계 경제·금리·환율 뉴스",
        },
        "KR_IT": {
            "label": "IT 동향",
            "description": "클라우드, 개발자 도구, 플랫폼 엔지니어링, 소프트웨어 업계 동향",
        },
        "KR_AI": {
            "label": "AI 뉴스",
            "description": "LLM, 생성형 AI, 에이전트, 모델, 오픈AI 이슈",
        },
        "KR_STACK": {
            "label": "최신 인기동향",
            "description": "벨로그 트렌딩 글과 Reddit hot 글로 보는 최신 개발 동향",
        },
    }


def _korean_category_label(category: str) -> str:
    return _korean_category_map()[category]["label"]


def _korean_category_description(category: str) -> str:
    return _korean_category_map()[category]["description"]


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
