from __future__ import annotations

import json
import os
import re
import tempfile
from threading import Lock, Thread
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.crawlers.rss_news import _html_to_text
from app.services.nasdaq_relevance import classify_nasdaq_relevance
from app.services.news_sources import collect_korean_news_from_sources
from app.services.telegram_notifier import (
    notify_market_news_digest,
    notify_new_investing_articles,
)


PROJECT_DATA_ROOT = Path(__file__).resolve().parents[3] / "data"
CACHE_TTL_SECONDS = int(os.getenv("NEWS_REFRESH_INTERVAL_SECONDS", "300"))
RETENTION_DAYS = int(os.getenv("NEWS_RETENTION_DAYS", "7"))
ARCHIVE_SCHEMA_VERSION = "2026-08-20-market-focus-v3"
DIGEST_INTERVAL = timedelta(minutes=15)
TOPIC_COOLDOWN = timedelta(minutes=30)
DEDUPLICATION_WINDOW = timedelta(hours=2)
EVENT_MARKER_PATTERN = re.compile(
    r"cpi|pce|fomc|fed|연준|고용|실업률|gdp|wti|브렌트|opec|나스닥|nasdaq",
    re.IGNORECASE,
)

_ARCHIVE_WRITE_LOCK = Lock()
_REFRESH_LOCK = Lock()
_REFRESH_WORK_LOCK = Lock()
_REFRESHING_CATEGORIES: set[str] = set()


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
        _schedule_refresh(category, limit)
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
    new_articles = _new_articles(archive["articles"], stored_articles)
    should_notify = _should_notify_new_investing_articles(archive, category, korean=True, now=now)

    archive["articles"] = _merge_articles(archive["articles"], stored_articles)
    archive["updated_at"] = _iso(now)
    alert_articles = _alert_articles(new_articles)
    if should_notify and alert_articles:
        notify_new_investing_articles(alert_articles)
    if should_notify:
        _queue_and_send_general_digest(archive, new_articles, now)
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
    displayed_articles = sorted_articles[:limit]
    relevance_summary = _relevance_summary(displayed_articles) if category == "KR_WORLD" else None
    return {
        "category": category,
        "label": label_resolver(category),
        "description": description_resolver(category),
        "count": len(displayed_articles),
        "articles": displayed_articles,
        "relevance_summary": relevance_summary,
        "cache": {
            "hit": cached,
            "age_seconds": age_seconds,
            "ttl_seconds": CACHE_TTL_SECONDS,
        },
    }


def _relevance_summary(articles: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"total": len(articles), "alert": 0, "archive": 0, "unclassified": 0}
    for article in articles:
        relevance = article.get("nasdaq_relevance")
        level = relevance.get("level") if isinstance(relevance, dict) else None
        if level == "alert":
            summary["alert"] += 1
        elif level == "archive":
            summary["archive"] += 1
        else:
            summary["unclassified"] += 1
    return summary


def _load_archive() -> dict[str, Any]:
    archive_path = _archive_path()
    if not archive_path.exists():
        return {"updated_at": "", "articles": [], "telegram_notifications_initialized": False}

    try:
        with archive_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"updated_at": "", "articles": [], "telegram_notifications_initialized": False}

    # Source changes must invalidate both the production and local archives.
    if data.get("schema_version") and data.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
        return {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "updated_at": "",
            "articles": [],
            "telegram_notifications_initialized": False,
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
        "telegram_notifications_initialized": bool(
            data.get("telegram_notifications_initialized", False)
        ),
        "telegram_last_digest_at": str(data.get("telegram_last_digest_at", "")),
        "telegram_pending_articles": _notification_articles(
            data.get("telegram_pending_articles", [])
        ),
        "telegram_recent_articles": _notification_articles(
            data.get("telegram_recent_articles", [])
        ),
        "telegram_topic_last_sent_at": _notification_times(
            data.get("telegram_topic_last_sent_at", {})
        ),
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


def _schedule_refresh(category: str, limit: int) -> None:
    with _REFRESH_LOCK:
        if category in _REFRESHING_CATEGORIES or _REFRESH_WORK_LOCK.locked():
            return
        _REFRESHING_CATEGORIES.add(category)

    def _run() -> None:
        try:
            with _REFRESH_WORK_LOCK:
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
        fresh_articles = collect_korean_news_from_sources(category=category, limit=limit)
    except Exception:
        fresh_articles = []
    stored_articles = [
        _attach_archive_metadata(article, category=category, now=now)
        for article in fresh_articles
    ]
    new_articles = _new_articles(archive["articles"], stored_articles)
    should_notify = _should_notify_new_investing_articles(archive, category, korean=True, now=now)

    archive["articles"] = _merge_articles(archive["articles"], stored_articles)
    archive["updated_at"] = _iso(now)
    alert_articles = _alert_articles(new_articles)
    if should_notify and alert_articles:
        notify_new_investing_articles(alert_articles)
    if should_notify:
        _queue_and_send_general_digest(archive, new_articles, now)
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


def _new_articles(existing_articles: list[dict[str, Any]], incoming_articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing_urls = {
        str(article.get("url", "")).strip()
        for article in existing_articles
        if str(article.get("url", "")).strip()
    }
    return [
        article
        for article in incoming_articles
        if str(article.get("url", "")).strip() not in existing_urls
    ]


def _should_notify_new_investing_articles(
    archive: dict[str, Any],
    category: str,
    korean: bool,
    now: datetime,
) -> bool:
    if not korean or category != "KR_WORLD":
        return False
    if not archive.get("telegram_notifications_initialized", False):
        archive["telegram_notifications_initialized"] = True
        archive["telegram_last_digest_at"] = _iso(now)
        return False
    return True


def _notification_articles(value: Any) -> list[dict[str, Any]]:
    return [dict(article) for article in value if isinstance(article, dict)] if isinstance(value, list) else []


def _notification_times(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(timestamp) for key, timestamp in value.items()}


def _queue_and_send_general_digest(
    archive: dict[str, Any], new_articles: list[dict[str, Any]], now: datetime
) -> None:
    pending = _notification_articles(archive.get("telegram_pending_articles", []))
    pending.extend(article for article in new_articles if not _alert_articles([article]))

    last_digest_at = _parse_dt(str(archive.get("telegram_last_digest_at", "")))
    if last_digest_at and now - last_digest_at < DIGEST_INTERVAL:
        archive["telegram_pending_articles"] = pending
        return

    selected, remaining = _select_general_digest_articles(
        pending,
        now=now,
        topic_last_sent_at=_notification_times(archive.get("telegram_topic_last_sent_at", {})),
        recent_sent_articles=_notification_articles(archive.get("telegram_recent_articles", [])),
    )
    if not selected:
        archive["telegram_pending_articles"] = remaining
        return
    if not notify_market_news_digest(selected):
        archive["telegram_pending_articles"] = selected + remaining
        return

    topic_times = _notification_times(archive.get("telegram_topic_last_sent_at", {}))
    for article in selected:
        topic_times[str(article["market_topic"])] = _iso(now)
    recent = _notification_articles(archive.get("telegram_recent_articles", [])) + [
        dict(article, sent_at=_iso(now)) for article in selected
    ]
    archive["telegram_pending_articles"] = remaining
    archive["telegram_recent_articles"] = [
        article
        for article in recent
        if (sent_at := _parse_dt(str(article.get("sent_at", ""))))
        and now - sent_at <= DEDUPLICATION_WINDOW
    ]
    archive["telegram_topic_last_sent_at"] = topic_times
    archive["telegram_last_digest_at"] = _iso(now)


def _select_general_digest_articles(
    pending: list[dict[str, Any]],
    now: datetime,
    topic_last_sent_at: dict[str, str],
    recent_sent_articles: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    selected_topics: set[str] = set()
    seen: list[dict[str, Any]] = list(recent_sent_articles)
    for article in pending:
        topic = _market_topic(article)
        candidate = dict(article, market_topic=topic)
        last_sent = _parse_dt(topic_last_sent_at.get(topic, ""))
        if len(selected) >= 3 or (last_sent and now - last_sent < TOPIC_COOLDOWN):
            remaining.append(article)
            continue
        if topic in selected_topics or any(_same_market_event(candidate, prior) for prior in seen):
            continue
        selected.append(candidate)
        selected_topics.add(topic)
        seen.append(candidate)
    return selected, remaining


def _market_topic(article: dict[str, Any]) -> str:
    text = f"{article.get('title_ko') or article.get('title') or ''} {article.get('summary') or ''}".casefold()
    if re.search(r"나스닥|nasdaq", text):
        return "나스닥"
    if re.search(r"원유|유가|국제유가|wti|브렌트|brent|opec", text):
        return "원유"
    if re.search(r"금\s*(?:값|가격|선물|시세|시장)|골드|gold|xau", text):
        return "금"
    return "미국"


def _same_market_event(current: dict[str, Any], previous: dict[str, Any]) -> bool:
    if _market_topic(current) != _market_topic(previous):
        return False
    if _has_new_market_detail(current, previous):
        return False
    current_key = _headline_key(current)
    previous_key = _headline_key(previous)
    if not current_key or not previous_key:
        return False
    if current_key == previous_key or current_key in previous_key or previous_key in current_key:
        return True

    similarity = _headline_bigram_similarity(current_key, previous_key)
    shared_markers = set(EVENT_MARKER_PATTERN.findall(current_key)) & set(
        EVENT_MARKER_PATTERN.findall(previous_key)
    )
    return similarity >= 0.6 or (bool(shared_markers) and similarity >= 0.25)


def _has_new_market_detail(current: dict[str, Any], previous: dict[str, Any]) -> bool:
    current_title = str(current.get("title_ko") or current.get("title") or "").casefold()
    previous_title = str(previous.get("title_ko") or previous.get("title") or "").casefold()
    current_numbers = set(re.findall(r"\d+(?:[.,]\d+)?%?", current_title))
    previous_numbers = set(re.findall(r"\d+(?:[.,]\d+)?%?", previous_title))
    if current_numbers - previous_numbers:
        return True

    decision_terms = {"결정", "동결", "인상", "인하", "확정", "실제"}
    return any(term in current_title and term not in previous_title for term in decision_terms)


def _headline_key(article: dict[str, Any]) -> str:
    title = str(article.get("title_ko") or article.get("title") or "").casefold()
    return re.sub(r"(?:발표|결과|시장|뉴스|속보|동향|마감)|[^0-9a-z가-힣]", "", title)


def _headline_bigram_similarity(left: str, right: str) -> float:
    left_bigrams = {left[index : index + 2] for index in range(len(left) - 1)}
    right_bigrams = {right[index : index + 2] for index in range(len(right) - 1)}
    if not left_bigrams or not right_bigrams:
        return 0.0
    return len(left_bigrams & right_bigrams) / len(left_bigrams | right_bigrams)


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
    stored["nasdaq_relevance"] = classify_nasdaq_relevance(stored)
    stored["collected_at"] = _iso(now)
    stored["expires_at"] = _iso(now + timedelta(days=RETENTION_DAYS))
    return stored


def _alert_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        article
        for article in articles
        if article.get("nasdaq_relevance", {}).get("level") == "alert"
        and article.get("nasdaq_relevance", {}).get("reasons")
    ]


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


def _normalize_korean_category(category: str) -> str:
    category = (category or "KR_WORLD").upper()
    return category if category in _korean_category_map() else "KR_WORLD"


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
