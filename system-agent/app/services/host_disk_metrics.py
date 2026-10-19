"""Export only the allow-listed Windows volume measured by the host collector."""

import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from app.services.metrics import DEFAULT_HOST_METRICS_PATH, DEFAULT_HOST_METRICS_STALE_SECONDS

LABELS = '{host="windows",volume="C:"}'
PREFIX = "personal_server_host_disk_"


def render_host_disk_metrics(path: Path | None = None, now: datetime | None = None) -> str:
    path = path if path is not None else Path(os.getenv("HOST_METRICS_PATH", DEFAULT_HOST_METRICS_PATH))
    now = now if now is not None else datetime.now(timezone.utc)
    samples = []
    healthy = False
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            raise ValueError("invalid_source")
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        disk = payload["host_disk"]
        if disk["source"] != "windows-cim" or disk["volume"] != "C:":
            raise ValueError("unverified_volume")
        capacity, available = disk["total_bytes"], disk["available_bytes"]
        if (type(capacity) is not int or type(available) is not int
                or not 0 <= available <= capacity <= 2**53 or capacity == 0):
            raise ValueError("invalid_capacity")
        raw_time = payload["captured_at"]
        if not isinstance(raw_time, str) or not re.fullmatch(r".+(?:Z|[+-]\d{2}:\d{2})", raw_time):
            raise ValueError("invalid_time")
        measured = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        age = (now - measured).total_seconds()
        if not math.isfinite(age) or age < -60:
            raise ValueError("future_time")
        healthy = age <= DEFAULT_HOST_METRICS_STALE_SECONDS
        samples = [("capacity_bytes", capacity), ("available_bytes", available),
                   ("sample_timestamp_seconds", measured.timestamp())]
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError):
        # Private paths, host names and malformed values never reach metric labels.
        pass
    samples.append(("collection_ok", int(healthy)))
    return "".join(f"# TYPE {PREFIX}{name} gauge\n{PREFIX}{name}{LABELS} {value}\n"
                   for name, value in samples)
