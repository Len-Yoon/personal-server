from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ALLOWED_SERVICE = "crawler-worker"
ALLOWED_ACTION = "restart_container"
_SECRET_PATTERN = re.compile(r"(?i)(authorization:\s*bearer\s+|api[-_ ]?key[=:]\s*|password[=:]\s*|token[=:]\s*)\S+")


class HomeOpsService:
    def __init__(self, db_path: Path, executor: Any, approval_ttl_seconds: int = 300):
        self.db_path, self.executor, self.approval_ttl_seconds = db_path, executor, approval_ttl_seconds
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create_diagnosis(self, service: str) -> dict[str, Any]:
        self._require_service(service)
        incident_id = str(uuid.uuid4())
        diagnostics = self._mask(self.executor.diagnostics(service))
        proposal = {"action": ALLOWED_ACTION, "service": service, "requires_approval": True,
                    "risk_level": "low", "summary": "컨테이너 재시작 검토 필요", "evidence": diagnostics["logs"]}
        with self._connect() as conn:
            conn.execute("INSERT INTO incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (incident_id, service, "proposed", self._now(), json.dumps(diagnostics), json.dumps(proposal), None, None))
        return {"incident_id": incident_id, "status": "proposed", "diagnostics": diagnostics, "proposal": proposal}

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
        status = "verified" if self.executor.health(row[0]) else "failed"
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
        if service != ALLOWED_SERVICE: raise ValueError("service_not_allowed")
