import os
import secrets

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from app.services import docker_ops
from app.services.idempotency import ClaimError, RestartStore, payload_hash
from app.services.runtime_state import runtime_contract_valid


app = FastAPI(title="HomeOps Restricted Executor")


class RestartRequest(BaseModel):
    incident_id: str = Field(min_length=1, max_length=128)
    approval_token: str = Field(min_length=1, max_length=1024)
    action: str
    service: str


def _require_shared_secret(provided: str) -> None:
    configured = _executor_shared_secret()
    if not configured or not secrets.compare_digest(provided, configured):
        raise HTTPException(status_code=403, detail="executor_access_denied")


def _executor_shared_secret() -> str:
    return os.getenv("HOMEOPS_EXECUTOR_SHARED_SECRET", "").strip()


@app.get("/health")
def health():
    return {"service": "homeops-executor", "status": "ok"}


@app.get("/ready")
def ready():
    checks = {
        "shared_auth": bool(_executor_shared_secret()),
        "runtime_contract": runtime_contract_valid() and bool(docker_ops.allowed_services()),
        "restart_state": RestartStore().ready(),
    }
    if not all(checks.values()):
        from fastapi.responses import JSONResponse
        return JSONResponse({"service": "homeops-executor", "status": "not_ready", "checks": checks}, status_code=503)
    return {"service": "homeops-executor", "status": "ready", "checks": checks}


def _claim(store: RestartStore, request_id: str, payload: dict):
    try:
        return store.claim(request_id, payload_hash(payload))
    except ClaimError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _complete(store: RestartStore, request_id: str, result: dict):
    try:
        store.complete(request_id, result)
    except ClaimError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@app.get("/v1/diagnostics/{service}")
def diagnostics(service: str, x_homeops_executor_secret: str = Header(default="")):
    _require_shared_secret(x_homeops_executor_secret)
    try:
        return docker_ops.collect_diagnostics(service)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/v1/diagnostics")
def all_diagnostics(response: Response, x_homeops_executor_secret: str = Header(default="")):
    _require_shared_secret(x_homeops_executor_secret)
    managed = docker_ops.allowed_services()
    response.headers["X-HomeOps-Managed-Services"] = ",".join(sorted(managed))
    return docker_ops.collect_all_diagnostics(managed_services=managed)


@app.post("/v1/restarts")
def restart(payload: RestartRequest, x_homeops_executor_secret: str = Header(default="")):
    _require_shared_secret(x_homeops_executor_secret)
    if not payload.incident_id.strip() or not payload.approval_token.strip():
        raise HTTPException(status_code=422, detail="restart_approval_required")
    if payload.action != "restart_container":
        raise HTTPException(status_code=403, detail="action_not_allowed")
    if payload.service not in docker_ops.allowed_services():
        raise HTTPException(status_code=403, detail="service_not_allowed")
    store = RestartStore()
    result = _claim(store, payload.incident_id, payload.model_dump())
    if result is not None:
        return result
    try:
        result = docker_ops.restart_service(payload.service)
    except ValueError as exc:
        # Leave the claim uncertain: an exception can occur after the restart.
        detail = str(exc)
        if detail in {"service_not_allowed", "service_container_not_found"}:
            raise HTTPException(status_code=403, detail=detail) from exc
        raise HTTPException(status_code=503, detail="restart_outcome_unknown") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="restart_outcome_unknown") from exc
    _complete(store, payload.incident_id, result)
    return result


def _restart_all_claimed(store: RestartStore, request_id: str) -> None:
    try:
        results = docker_ops.restart_all_services()
        if any(item.get("status") == "failed" for item in results):
            return  # Possibly partial: retain uncertain claim and never repeat.
        store.complete(request_id, {"status": "accepted"})
    except Exception:
        # Process death and operation errors have the same fail-closed replay.
        return


@app.post("/v1/restarts/all")
def restart_all(background_tasks: BackgroundTasks, x_homeops_executor_secret: str = Header(default=""),
                x_homeops_request_id: str = Header(default="")):
    _require_shared_secret(x_homeops_executor_secret)
    if not x_homeops_request_id.strip() or len(x_homeops_request_id) > 128:
        raise HTTPException(status_code=422, detail="restart_request_id_required")
    store = RestartStore()
    result = _claim(store, x_homeops_request_id, {"action": "restart_all", "service": "all"})
    if result is not None:
        return result
    background_tasks.add_task(_restart_all_claimed, store, x_homeops_request_id)
    return {"status": "accepted"}
