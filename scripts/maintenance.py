#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.getenv("DATA_ROOT", PROJECT_ROOT / "data"))
BACKUP_ROOT = Path(os.getenv("BACKUP_PATH", DATA_ROOT / "backups"))
BACKUP_RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "14"))
SECURITY_LOG_PATH = Path(os.getenv("SECURITY_LOG_PATH", DATA_ROOT / "logs" / "security-events.txt"))
SECURITY_LOG_RETENTION_DAYS = int(os.getenv("SECURITY_LOG_RETENTION_DAYS", "30"))
NEWS_RETENTION_DAYS = int(os.getenv("NEWS_RETENTION_DAYS", "7"))


def backup() -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_ROOT / stamp
    target.mkdir(parents=True, exist_ok=True)

    sqlite_files = sorted(DATA_ROOT.glob("*/*.sqlite3"))
    for source in sqlite_files:
        destination = target / source.parent.name / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        _backup_sqlite(source, destination)
        print(f"backed up sqlite: {source} -> {destination}")

    if _truthy(os.getenv("BACKUP_INCLUDE_FILES", "")):
        files_root = DATA_ROOT / "files"
        if files_root.exists():
            archive_base = target / "files"
            shutil.make_archive(str(archive_base), "zip", files_root)
            print(f"backed up files: {archive_base}.zip")

    _prune_directories(BACKUP_ROOT, BACKUP_RETENTION_DAYS)


def prune_logs() -> None:
    cutoff = datetime.now() - timedelta(days=SECURITY_LOG_RETENTION_DAYS)
    for log_file in _security_log_files():
        if datetime.fromtimestamp(log_file.stat().st_mtime) < cutoff:
            log_file.unlink()
            print(f"removed old log: {log_file}")


def prune_news_archive() -> int:
    retention_days = int(os.getenv("NEWS_RETENTION_DAYS", str(NEWS_RETENTION_DAYS)))
    if retention_days < 0:
        raise ValueError("NEWS_RETENTION_DAYS must be non-negative")

    archive_path = Path(
        os.getenv(
            "NEWS_ARCHIVE_PATH",
            DATA_ROOT / "crawler-worker" / "news_archive.json",
        )
    )
    if not archive_path.exists():
        return 0

    try:
        archive = json.loads(archive_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0

    articles = archive.get("articles", [])
    if not isinstance(articles, list):
        return 0

    today = datetime.now().date()
    cutoff = today - timedelta(days=retention_days)
    kept = []
    removed = 0
    for article in articles:
        if not isinstance(article, dict):
            removed += 1
            continue
        expires_at = _parse_archive_datetime(article.get("expires_at"))
        collected_at = _parse_archive_datetime(article.get("collected_at"))
        expired = (
            expires_at.date() < today
            if expires_at
            else collected_at.date() < cutoff if collected_at else False
        )
        if expired:
            removed += 1
        else:
            kept.append(article)

    if removed:
        archive["articles"] = kept
        archive["updated_at"] = datetime.now().isoformat()
        archive_path.write_text(
            json.dumps(archive, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"removed expired news articles: {removed}")
    return removed


def _parse_archive_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _backup_sqlite(source: Path, destination: Path) -> None:
    source_uri = f"file:{source}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_db:
        with sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)


def _security_log_files() -> list[Path]:
    if not SECURITY_LOG_PATH.parent.exists():
        return []
    if SECURITY_LOG_PATH.suffix:
        pattern = f"{SECURITY_LOG_PATH.stem}-*{SECURITY_LOG_PATH.suffix}"
    else:
        pattern = "security-events-*.txt"
    return sorted(SECURITY_LOG_PATH.parent.glob(pattern))


def _prune_directories(path: Path, retention_days: int) -> None:
    if not path.exists():
        return
    cutoff = datetime.now() - timedelta(days=retention_days)
    for child in path.iterdir():
        if child.is_dir() and datetime.fromtimestamp(child.stat().st_mtime) < cutoff:
            shutil.rmtree(child)
            print(f"removed old backup: {child}")


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    parser = argparse.ArgumentParser(description="personal-server maintenance utilities")
    parser.add_argument(
        "command",
        choices=[
            "backup",
            "prune-logs",
            "prune-news",
            "all",
        ],
    )
    args = parser.parse_args()

    if args.command in {"backup", "all"}:
        backup()
    if args.command in {"prune-logs", "all"}:
        prune_logs()
    if args.command in {"prune-news", "all"}:
        prune_news_archive()


if __name__ == "__main__":
    main()
