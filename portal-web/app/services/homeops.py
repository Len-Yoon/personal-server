from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import uuid
import os
import time
from urllib.request import Request, urlopen
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ALLOWED_SERVICES = frozenset({"portal-web", "system-agent", "crawler-worker", "youtube-memo", "book-memo", "caddy", "homeops-executor"})
ALLOWED_ACTION = "restart_container"
AUTO_RESTART_COOLDOWN_SECONDS = 600
_SECRET_PATTERN = re.compile(r"(?i)(authorization:\s*bearer\s+|api[-_ ]?key[=:]\s*|password[=:]\s*|token[=:]\s*)\S+")
PROJECT_DATA_ROOT = next((parent / "data" for parent in Path(__file__).resolve().parents if (parent / "docker-compose.yml").exists()), Path("/app/data"))


class HomeOpsService:
    def __init__(self, db_path: Path, executor: Any, approval_ttl_seconds: int = 300, verification_attempts: int = 5, verification_interval_seconds: float = 2):
        self.db_path, self.executor, self.approval_ttl_seconds = db_path, executor, approval_ttl_seconds
        self.verification_attempts, self.verification_interval_seconds = verification_attempts, verification_interval_seconds
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create_diagnosis(self, service: str) -> dict[str, Any]:
        self._require_service(service)
        incident_id = str(uuid.uuid4())
        diagnostics = self._mask(self.executor.diagnostics(service))
        unhealthy = diagnostics.get("container", {}).get("status") != "running" or diagnostics.get("container", {}).get("health") == "unhealthy"
        proposal = {"action": ALLOWED_ACTION if unhealthy else "no_action", "service": service, "requires_approval": bool(unhealthy),
                    "risk_level": "medium" if unhealthy else "low", "summary": "컨테이너 재시작 검토 필요" if unhealthy else "정상 상태: 조치 불필요", "evidence": diagnostics["logs"]}
        with self._connect() as conn:
            conn.execute("INSERT INTO incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (incident_id, service, "proposed", self._now(), json.dumps(diagnostics), json.dumps(proposal), None, None))
        if unhealthy and self._consecutive_unhealthy(service) >= 3:
            self.approve_incident(incident_id, "homeops-policy")
            self.execute_approved_incident(incident_id)
        return {"incident_id": incident_id, "status": "proposed", "diagnostics": diagnostics, "proposal": proposal}

    def _consecutive_unhealthy(self, service: str) -> int:
        with self._connect() as conn:
            rows = conn.execute("SELECT proposal FROM incidents WHERE service=? ORDER BY created_at DESC LIMIT 3", (service,)).fetchall()
            last_recovery = conn.execute("SELECT completed_at FROM incidents WHERE service=? AND status='verified' ORDER BY completed_at DESC LIMIT 1", (service,)).fetchone()
        if last_recovery and datetime.fromisoformat(last_recovery[0]) + timedelta(seconds=AUTO_RESTART_COOLDOWN_SECONDS) > datetime.now(timezone.utc):
            return 0
        return len(rows) if len(rows) == 3 and all(json.loads(row[0]).get("action") == ALLOWED_ACTION for row in rows) else 0

    def approve_incident(self, incident_id: str, approved_by: str) -> dict[str, str]:
        token = secrets.token_urlsafe(32)
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=self.approval_ttl_seconds)).isoformat()
        with self._connect() as conn:
            updated = conn.execute("UPDATE incidents SET status='approved', approved_by=? WHERE incident_id=? AND status='proposed'", (approved_by, incident_id)).rowcount
            if not updated:
                return {"status": "failed", "reason": "incident_not_proposed"}
            conn.execute("INSERT INTO approval_tokens VALUES (?, ?, ?, NULL)", (incident_id, self._hash(token), expiry))
        return {"status": "approved", "approval_token": token}

    def execute_approved_incident(self, incident_id: str) -> dict[str, str]:
        with self._connect() as conn:
            row = conn.execute("SELECT service,status FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
            token_row = conn.execute("SELECT token_hash,expires_at,consumed_at FROM approval_tokens WHERE incident_id=?", (incident_id,)).fetchone()
            if not row or row[1] != "approved" or not token_row or token_row[2] or token_row[1] < self._now():
                return {"status": "failed", "reason": "approval_not_available"}
            conn.execute("UPDATE approval_tokens SET consumed_at=? WHERE incident_id=? AND consumed_at IS NULL", (self._now(), incident_id))
            conn.execute("UPDATE incidents SET status='executing' WHERE incident_id=?", (incident_id,))
        token = "executor-token"  # Executor authentication is internal; approval consumption is enforced above.
        self.executor.restart(incident_id, token, row[0])
        status = "failed"
        for attempt in range(self.verification_attempts):
            if self.executor.health(row[0]):
                status = "verified"
                break
            if attempt < self.verification_attempts - 1:
                time.sleep(self.verification_interval_seconds)
        with self._connect() as conn:
            conn.execute("UPDATE incidents SET status=?, completed_at=? WHERE incident_id=?", (status, self._now(), incident_id))
        return {"status": status, "incident_id": incident_id}

    def list_incidents(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT incident_id,service,status,created_at,proposal FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [{"incident_id": row[0], "service": row[1], "status": row[2], "created_at": row[3], "proposal": json.loads(row[4])} for row in rows]

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS incidents (incident_id TEXT PRIMARY KEY, service TEXT, status TEXT, created_at TEXT, diagnostics TEXT, proposal TEXT, approved_by TEXT, completed_at TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS approval_tokens (incident_id TEXT PRIMARY KEY, token_hash TEXT, expires_at TEXT, consumed_at TEXT)")

    def _connect(self):
        return sqlite3.connect(self.db_path)

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _mask(value: Any) -> Any:
        if isinstance(value, str): return _SECRET_PATTERN.sub(r"\1[REDACTED]", value)
        if isinstance(value, list): return [HomeOpsService._mask(item) for item in value]
        if isinstance(value, dict): return {key: HomeOpsService._mask(item) for key, item in value.items()}
        return value

    @staticmethod
    def _require_service(service: str) -> None:
        if service not in ALLOWED_SERVICES: raise ValueError("service_not_allowed")


class ExecutorClient:
    def __init__(self):
        self.url = os.getenv("HOMEOPS_EXECUTOR_URL", "http://homeops-executor:8011").rstrip("/")
        self.secret = os.getenv("HOMEOPS_EXECUTOR_SHARED_SECRET", "")

    def diagnostics(self, service: str) -> dict[str, Any]:
        return self._request(f"/v1/diagnostics/{service}")

    def restart(self, incident_id: str, approval_token: str, service: str) -> dict[str, Any]:
        return self._request("/v1/restarts", {"incident_id": incident_id, "approval_token": approval_token, "action": ALLOWED_ACTION, "service": service})

    def health(self, service: str) -> bool:
        return bool(self.diagnostics(service).get("container", {}).get("health") == "healthy")

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode() if payload else None
        request = Request(self.url + path, data=data, headers={"X-HomeOps-Executor-Secret": self.secret, "Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode())


def get_homeops_service() -> HomeOpsService:
    data_root = Path(os.getenv("HOMEOPS_DB_PATH", str(PROJECT_DATA_ROOT / "logs" / "homeops.sqlite3")))
    return HomeOpsService(data_root, ExecutorClient())
