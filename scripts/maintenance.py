#!/usr/bin/env python3
import argparse
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
    parser.add_argument("command", choices=["backup", "prune-logs", "all"])
    args = parser.parse_args()

    if args.command in {"backup", "all"}:
        backup()
    if args.command in {"prune-logs", "all"}:
        prune_logs()


if __name__ == "__main__":
    main()
