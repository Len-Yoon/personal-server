import os
import json
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any, Callable


PROJECT_DATA_ROOT = Path(__file__).resolve().parents[3] / "data"


def empty_archive() -> dict[str, object]:
    return {
        "updated_at": "",
        "articles": [],
        "telegram_notifications_initialized": False,
    }


def archive_path() -> Path:
    return Path(
        os.getenv(
            "NEWS_ARCHIVE_PATH",
            PROJECT_DATA_ROOT / "crawler-worker" / "news_archive.json",
        )
    )


def load_archive(
    path: Path,
    schema_version: str,
    sanitize_article: Callable[[dict[str, Any]], dict[str, Any]],
    notification_articles: Callable[[Any], list[dict[str, Any]]],
    notification_times: Callable[[Any], dict[str, str]],
    save_archive: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    if not path.exists():
        return empty_archive()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return empty_archive()
    if data.get("schema_version") and data.get("schema_version") != schema_version:
        return {
            "schema_version": schema_version,
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
        normalized = sanitize_article(article)
        if normalized != article:
            changed = True
        normalized_articles.append(normalized)

    archive = {
        "schema_version": str(data.get("schema_version", schema_version)),
        "updated_at": str(data.get("updated_at", "")),
        "articles": normalized_articles,
        "telegram_notifications_initialized": bool(data.get("telegram_notifications_initialized", False)),
        "telegram_last_digest_at": str(data.get("telegram_last_digest_at", "")),
        "telegram_pending_articles": notification_articles(data.get("telegram_pending_articles", [])),
        "telegram_recent_articles": notification_articles(data.get("telegram_recent_articles", [])),
        "telegram_topic_last_sent_at": notification_times(data.get("telegram_topic_last_sent_at", {})),
    }
    if changed:
        try:
            save_archive(archive)
        except OSError:
            pass
    return archive


def save_archive(
    archive: dict[str, Any],
    path: Path,
    schema_version: str,
    write_lock: Lock,
) -> None:
    archive["schema_version"] = schema_version
    with write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            json.dump(archive, handle, ensure_ascii=False, indent=2)
            temp_path = Path(handle.name)
        temp_path.replace(path)
