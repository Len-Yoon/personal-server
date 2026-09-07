from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any


_STATUS_KEYS = (
    "initialized",
    "first_attempt_at",
    "last_attempt_at",
    "last_success_at",
    "last_failure_at",
    "failures_total",
    "consecutive_failures",
)


class NewsCollectionStatusStore:
    """Atomic, restart-safe, secret-free state for scheduled news collection."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path or _default_path())
        self._lock = Lock()

    def record_attempt(self, at: datetime | None = None) -> None:
        with self._lock:
            state = self._load()
            state["initialized"] = True
            if state["first_attempt_at"] is None:
                state["first_attempt_at"] = _iso(at or _now())
            state["last_attempt_at"] = _iso(at or _now())
            self._save(state)

    def record_success(self, at: datetime | None = None) -> None:
        with self._lock:
            state = self._load()
            state["initialized"] = True
            state["last_success_at"] = _iso(at or _now())
            state["consecutive_failures"] = 0
            self._save(state)

    def record_failure(self, at: datetime | None = None) -> None:
        with self._lock:
            state = self._load()
            state["initialized"] = True
            state["last_failure_at"] = _iso(at or _now())
            state["failures_total"] += 1
            state["consecutive_failures"] += 1
            self._save(state)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._load()

    def _load(self) -> dict[str, Any]:
        state = _default_state()
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return state
        if not isinstance(payload, dict):
            return state
        for key in _STATUS_KEYS:
            value = payload.get(key)
            valid = (
                isinstance(value, bool) if key == "initialized"
                else isinstance(value, int) and not isinstance(value, bool)
                if key in {"failures_total", "consecutive_failures"}
                else value is None or isinstance(value, str)
            )
            if valid:
                state[key] = value
        return state

    def _save(self, state: dict[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{self._path.name}.", dir=self._path.parent)
        except OSError:
            return
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({key: state[key] for key in _STATUS_KEYS}, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _default_path() -> str:
    configured = os.getenv("NEWS_COLLECTION_STATUS_PATH")
    if configured:
        return configured
    archive_path = os.getenv("NEWS_ARCHIVE_PATH")
    if archive_path:
        return str(Path(archive_path).with_name("news_collection_status.json"))
    return "/data/crawler-worker/news_collection_status.json"


def _default_state() -> dict[str, Any]:
    return {
        "initialized": False,
        "first_attempt_at": None,
        "last_attempt_at": None,
        "last_success_at": None,
        "last_failure_at": None,
        "failures_total": 0,
        "consecutive_failures": 0,
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
