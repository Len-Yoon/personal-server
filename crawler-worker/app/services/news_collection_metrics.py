from __future__ import annotations

from datetime import datetime
from typing import Any


METRIC_NAMES = (
    "crawler_news_collection_initialized",
    "crawler_news_collection_last_attempt_timestamp_seconds",
    "crawler_news_collection_first_attempt_timestamp_seconds",
    "crawler_news_collection_last_success_timestamp_seconds",
    "crawler_news_collection_last_failure_timestamp_seconds",
    "crawler_news_collection_failures_total",
    "crawler_news_collection_consecutive_failures",
    "crawler_news_refresh_interval_seconds",
)


def render_metrics(snapshot: dict[str, Any], refresh_interval_seconds: int = 300) -> str:
    values = {
        "crawler_news_collection_initialized": 1 if snapshot.get("initialized") else 0,
        "crawler_news_collection_last_attempt_timestamp_seconds": _timestamp(snapshot.get("last_attempt_at")),
        "crawler_news_collection_first_attempt_timestamp_seconds": _timestamp(snapshot.get("first_attempt_at")),
        "crawler_news_collection_last_success_timestamp_seconds": _timestamp(snapshot.get("last_success_at")),
        "crawler_news_collection_last_failure_timestamp_seconds": _timestamp(snapshot.get("last_failure_at")),
        "crawler_news_collection_failures_total": _integer(snapshot.get("failures_total")),
        "crawler_news_collection_consecutive_failures": _integer(snapshot.get("consecutive_failures")),
        "crawler_news_refresh_interval_seconds": max(1, int(refresh_interval_seconds)),
    }
    return "".join(f"{name} {values[name]}\n" for name in METRIC_NAMES)


def _timestamp(value: Any) -> int:
    if not isinstance(value, str) or not value:
        return 0
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0


def _integer(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
