import json
import os
from pathlib import Path

from fastapi import FastAPI

from app.services.metrics import collect_metrics, demo_metrics


app = FastAPI(title="Personal Server System Agent")


RECOVERY_EVENT_FIELDS = ("timestamp", "component", "event", "status", "action")
RECOVERY_EVENT_LIMIT = 10


@app.get("/health")
def health():
    return {
        "service": "system-agent",
        "status": "ok",
    }


@app.get("/metrics")
def metrics():
    if _truthy(os.getenv("DEMO_MODE", "")):
        return demo_metrics()
    return collect_metrics()


@app.get("/metrics/demo")
def metrics_demo():
    return demo_metrics()


@app.get("/recovery-events")
def recovery_events() -> list[dict[str, str]]:
    return _read_recovery_events()


def _read_recovery_events() -> list[dict[str, str]]:
    try:
        events_path = Path(os.getenv("DATA_ROOT", "/data")) / "recovery-events.jsonl"
        entries: list[dict[str, str]] = []
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if not isinstance(entry, dict) or not all(
                isinstance(entry.get(field), str) for field in RECOVERY_EVENT_FIELDS
            ):
                return []
            entries.append({field: entry[field] for field in RECOVERY_EVENT_FIELDS})
        return entries[-RECOVERY_EVENT_LIMIT:]
    except (OSError, UnicodeError, ValueError, TypeError):
        return []


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
