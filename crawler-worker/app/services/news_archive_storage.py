import os
import json
import tempfile
import logging
from pathlib import Path
from threading import Lock
from typing import Any, Callable


PROJECT_DATA_ROOT = Path(__file__).resolve().parents[3] / "data"
PREVIOUS_SCHEMA_VERSION = "2026-07-15-korean-news-v2"
LOGGER = logging.getLogger(__name__)
KNOWN_TELEGRAM_FIELDS = {
    "telegram_notifications_initialized", "telegram_last_digest_at",
    "telegram_pending_articles", "telegram_recent_articles",
    "telegram_topic_last_sent_at", "telegram_outbox",
}


def _invalid_archive(reason: str) -> None:
    LOGGER.error("news archive unavailable: %s; original preserved", reason)
    raise ValueError(f"news archive {reason}; original preserved")


def _backup_legacy(path: Path, original: bytes) -> None:
    backup = path.with_name(f"{path.name}.v2.bak")
    try:
        with backup.open("xb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        _sync_directory(path.parent)
    except FileExistsError:
        if backup.read_bytes() != original:
            _invalid_archive("v2 backup differs from source")
        with backup.open("rb") as handle:
            os.fsync(handle.fileno())
        _sync_directory(path.parent)
    if backup.read_bytes() != original:
        raise OSError("news archive v2 backup verification failed")


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def empty_archive() -> dict[str, object]:
    return {
        "updated_at": "",
        "articles": [],
        "telegram_notifications_initialized": False,
        "telegram_outbox": [],
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
    notification_outbox: Callable[[Any], list[dict[str, Any]]],
    save_archive: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    if not path.exists():
        return empty_archive()
    original = path.read_bytes()
    try:
        data = json.loads(original.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        LOGGER.error("news archive unavailable: corrupt JSON; original preserved")
        raise ValueError("news archive is corrupt; original preserved") from exc
    if not isinstance(data, dict) or not isinstance(data.get("articles", []), list):
        _invalid_archive("has invalid top-level data")
    version = data.get("schema_version")
    if version not in (None, "", schema_version, PREVIOUS_SCHEMA_VERSION):
        _invalid_archive("has unsupported schema")
    if any(key.startswith("telegram_") and key not in KNOWN_TELEGRAM_FIELDS for key in data):
        _invalid_archive("contains unknown notification fields")
    for key in ("telegram_pending_articles", "telegram_recent_articles"):
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            _invalid_archive("has invalid notification data")
    if not isinstance(data.get("telegram_topic_last_sent_at", {}), dict):
        _invalid_archive("has invalid notification times")
    raw_outbox = data.get("telegram_outbox", [])
    if not isinstance(raw_outbox, list):
        LOGGER.warning("news archive contains malformed outbox; invalid entries ignored")
        raw_outbox = []
    known_event_fields = {"event_id", "kind", "status", "articles", "article_urls", "created_at", "removed_urls", "sent_at"}
    if any(set(event) - known_event_fields for event in raw_outbox if isinstance(event, dict)):
        _invalid_archive("contains unknown notification event fields")
    if len(notification_outbox(raw_outbox)) != len(raw_outbox):
        LOGGER.warning("news archive contains malformed outbox entries; invalid entries ignored")
    if version == PREVIOUS_SCHEMA_VERSION:
        _backup_legacy(path, original)
        data = dict(data, schema_version=schema_version, articles=[], telegram_notifications_initialized=False)
        save_archive(data)

    articles = data.get("articles", [])
    normalized_articles = []
    changed = False
    for article in articles:
        if not isinstance(article, dict):
            _invalid_archive("has invalid article data")
        normalized = sanitize_article(article)
        if normalized != article:
            changed = True
        normalized_articles.append(normalized)

    archive = dict(data)
    archive.update({
        "schema_version": str(data.get("schema_version", schema_version)),
        "updated_at": str(data.get("updated_at", "")),
        "articles": normalized_articles,
        "telegram_notifications_initialized": bool(data.get("telegram_notifications_initialized", False)),
        "telegram_last_digest_at": str(data.get("telegram_last_digest_at", "")),
        "telegram_pending_articles": notification_articles(data.get("telegram_pending_articles", [])),
        "telegram_recent_articles": notification_articles(data.get("telegram_recent_articles", [])),
        "telegram_topic_last_sent_at": notification_times(data.get("telegram_topic_last_sent_at", {})),
        "telegram_outbox": notification_outbox(data.get("telegram_outbox", [])),
    })
    if changed:
        save_archive(archive)
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
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        temp_path.replace(path)
        _sync_directory(path.parent)
