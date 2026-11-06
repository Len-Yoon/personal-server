import os
import secrets

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from app.services import docker_ops
from app.services.idempotency import ClaimError, RestartStore, payload_hash
from app.services.runtime_state import runtime_contract_valid
from app.services.ha_backend import runtime
from contextlib import nullcontext


app = FastAPI(title="HomeOps Restricted Executor")
if (backend := runtime()) is not None:
    backend.register_fastapi(app)


class RestartRequest(BaseModel):
    incident_id: str = Field(min_length=1, max_length=128)
    approval_token: str = Field(min_length=1, max_length=1024)
    action: str
    service: str
    target_node: str | None = None


def _require_target(target: str | None) -> None:
    if (backend := runtime()) is not None:
        if not target or target != backend.node_id():
            raise HTTPException(status_code=403, detail="executor_target_mismatch")


def _operation_scope():
    backend = runtime()
    return backend.worker_scope("homeops-executor") if backend is not None else nullcontext()


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
def diagnostics(service: str, x_homeops_executor_secret: str = Header(default=""),
                x_homeops_target_node: str = Header(default="")):
    _require_shared_secret(x_homeops_executor_secret)
    _require_target(x_homeops_target_node)
    try:
        return docker_ops.collect_diagnostics(service)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/v1/diagnostics")
def all_diagnostics(response: Response, x_homeops_executor_secret: str = Header(default=""),
                    x_homeops_target_node: str = Header(default="")):
    _require_shared_secret(x_homeops_executor_secret)
    _require_target(x_homeops_target_node)
    managed = docker_ops.allowed_services()
    response.headers["X-HomeOps-Managed-Services"] = ",".join(sorted(managed))
    return docker_ops.collect_all_diagnostics(managed_services=managed)


@app.post("/v1/restarts")
def restart(payload: RestartRequest, x_homeops_executor_secret: str = Header(default="")):
    _require_shared_secret(x_homeops_executor_secret)
    _require_target(payload.target_node)
    if not payload.incident_id.strip() or not payload.approval_token.strip():
        raise HTTPException(status_code=422, detail="restart_approval_required")
    if payload.action != "restart_container":
        raise HTTPException(status_code=403, detail="action_not_allowed")
    if payload.service not in docker_ops.allowed_services():
        raise HTTPException(status_code=403, detail="service_not_allowed")
    with _operation_scope():
        return _restart_approved(payload)


def _restart_approved(payload: RestartRequest):
    store = RestartStore()
    claim_payload = payload.model_dump()
    if runtime() is None:
        claim_payload.pop("target_node", None)
    result = _claim(store, payload.incident_id, claim_payload)
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
        with _operation_scope():
            results = docker_ops.restart_all_services()
            if any(item.get("status") == "failed" for item in results):
                return  # Possibly partial: retain uncertain claim and never repeat.
            store.complete(request_id, {"status": "accepted"})
    except Exception:
        # Process death and operation errors have the same fail-closed replay.
        return


@app.post("/v1/restarts/all")
def restart_all(background_tasks: BackgroundTasks, x_homeops_executor_secret: str = Header(default=""),
                x_homeops_request_id: str = Header(default=""),
                x_homeops_target_node: str = Header(default="")):
    _require_shared_secret(x_homeops_executor_secret)
    _require_target(x_homeops_target_node)
    if not x_homeops_request_id.strip() or len(x_homeops_request_id) > 128:
        raise HTTPException(status_code=422, detail="restart_request_id_required")
    with _operation_scope():
        return _restart_all_approved(background_tasks, x_homeops_request_id, x_homeops_target_node)


def _restart_all_approved(background_tasks, request_id: str, target_node: str):
    store = RestartStore()
    claim_payload = {"action": "restart_all", "service": "all"}
    if runtime() is not None:
        claim_payload["target_node"] = target_node
    result = _claim(store, request_id, claim_payload)
    if result is not None:
        return result
    if runtime() is not None:
        # Keep the same generation permit from claim through the final effect.
        _restart_all_claimed(store, request_id)
    else:
        background_tasks.add_task(_restart_all_claimed, store, request_id)
    return {"status": "accepted"}
