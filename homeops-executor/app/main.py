import os
import secrets

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from pydantic import BaseModel

from app.services import docker_ops


app = FastAPI(title="HomeOps Restricted Executor")


class RestartRequest(BaseModel):
    incident_id: str
    approval_token: str
    action: str
    service: str


def _require_shared_secret(provided: str) -> None:
    configured = _executor_shared_secret()
    if not configured or not secrets.compare_digest(provided, configured):
        raise HTTPException(status_code=403, detail="executor_access_denied")


def _executor_shared_secret() -> str:
    return (
        os.getenv("HOMEOPS_EXECUTOR_SHARED_SECRET", "").strip()
        or os.getenv("ADMIN_STATUS_PASSWORD", "").strip()
    )


@app.get("/health")
def health():
    return {"service": "homeops-executor", "status": "ok"}


@app.get("/v1/diagnostics/{service}")
def diagnostics(service: str, x_homeops_executor_secret: str = Header(default="")):
    _require_shared_secret(x_homeops_executor_secret)
    try:
        return docker_ops.collect_diagnostics(service)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/v1/diagnostics")
def all_diagnostics(x_homeops_executor_secret: str = Header(default="")):
    _require_shared_secret(x_homeops_executor_secret)
    return docker_ops.collect_all_diagnostics()


@app.post("/v1/restarts")
def restart(payload: RestartRequest, x_homeops_executor_secret: str = Header(default="")):
    _require_shared_secret(x_homeops_executor_secret)
    if payload.action != "restart_container":
        raise HTTPException(status_code=403, detail="action_not_allowed")
    try:
        return docker_ops.restart_service(payload.service)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/v1/restarts/all")
def restart_all(background_tasks: BackgroundTasks, x_homeops_executor_secret: str = Header(default="")):
    _require_shared_secret(x_homeops_executor_secret)
    background_tasks.add_task(docker_ops.restart_all_services)
    return {"status": "accepted"}
