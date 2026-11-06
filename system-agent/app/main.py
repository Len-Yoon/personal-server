import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.responses import PlainTextResponse

from app.services.metrics import collect_metrics, demo_metrics
from app.services.ha_backend import runtime


app = FastAPI(title="Personal Server System Agent")
if (backend := runtime()) is not None:
    backend.register_fastapi(app)


@app.middleware("http")
async def node_scope(request: Request, call_next):
    backend = runtime()
    if backend is not None:
        try:
            identity = backend.node_id()
        except backend.DatabaseUnavailable:
            return JSONResponse({"detail": "agent_identity_unavailable"}, status_code=503)
        # Liveness is discoverable; host observations require an explicit node.
        if request.url.path != "/health" and request.headers.get("x-ha-target-node") != identity:
            return JSONResponse({"detail": "agent_target_mismatch"}, status_code=403)
        response = await call_next(request)
        response.headers["X-HA-Node-ID"] = identity
        response.headers["Cache-Control"] = "no-store"
        return response
    return await call_next(request)


RECOVERY_EVENT_FIELDS = ("timestamp", "component", "event", "status", "action")
RECOVERY_EVENT_LIMIT = 10


@app.get("/health")
def health():
    result = {
        "service": "system-agent",
        "status": "ok",
    }
    if (backend := runtime()) is not None:
        result["node_id"] = backend.node_id()
    return result


@app.get("/metrics")
def metrics():
    if _truthy(os.getenv("DEMO_MODE", "")):
        return demo_metrics()
    return collect_metrics()


@app.get("/metrics/demo")
def metrics_demo():
    return demo_metrics()


@app.get("/internal/metrics", response_class=PlainTextResponse)
def prometheus_metrics():
    from app.services.host_disk_metrics import render_host_disk_metrics
    return PlainTextResponse(render_host_disk_metrics(),
                             media_type="text/plain; version=0.0.4; charset=utf-8")


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
