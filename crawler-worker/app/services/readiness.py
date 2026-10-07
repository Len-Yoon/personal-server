"""Read-only local readiness checks, independent from liveness and upstream RSS."""
import json
import os
from pathlib import Path

from app.services.news_archive import ARCHIVE_SCHEMA_VERSION
from app.services.news_archive_storage import archive_path, validate_archive_payload
from app.services.news_collection_status import _default_path


def _state_available(path: Path, *, archive: bool = False) -> bool:
    try:
        # An empty but provisioned volume is ready for its first collection.
        if not path.parent.is_dir() or not os.access(path.parent, os.W_OK | os.X_OK):
            return False
        if not path.exists():
            return True
        if not path.is_file() or not os.access(path, os.R_OK | os.W_OK):
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        if archive:
            validate_archive_payload(payload, ARCHIVE_SCHEMA_VERSION)
            return True
        for key in ("failures_total", "consecutive_failures"):
            if key in payload and (not isinstance(payload[key], int) or isinstance(payload[key], bool) or payload[key] < 0):
                return False
        if "initialized" in payload and not isinstance(payload["initialized"], bool):
            return False
        return True
    except (OSError, ValueError, UnicodeError):
        return False


def readiness_checks() -> dict[str, bool]:
    return {"archive_state": _state_available(archive_path(), archive=True),
            "collection_state": _state_available(Path(_default_path()))}
