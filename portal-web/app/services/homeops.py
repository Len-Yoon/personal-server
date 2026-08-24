from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import uuid
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ALLOWED_SERVICES = frozenset({"portal-web", "system-agent", "crawler-worker", "youtube-memo", "book-memo", "caddy", "homeops-executor"})
ALLOWED_ACTION = "restart_container"
AUTO_RESTART_COOLDOWN_SECONDS = 600
AUTO_RESTART_MAX_PER_HOUR = 2
HOST_MEMORY_ALERT_PERCENT = 90.0
HOST_MEMORY_ALERT_CONSECUTIVE_SAMPLES = 3
CONTAINER_CPU_RESTART_PERCENT = 85.0
CONTAINER_MEMORY_RESTART_PERCENT = 90.0
RESTART_VERIFICATION_DELAY_SECONDS = 20
_CRITICAL_LOG_PATTERN = re.compile(r"(?i)\b(fatal|panic|oom|out of memory|memoryerror|segmentation fault|connection refused)\b")
_SECRET_PATTERN = re.compile(r"(?i)(authorization:\s*bearer\s+|api[-_ ]?key[=:]\s*|password[=:]\s*|token[=:]\s*)\S+")
PROJECT_DATA_ROOT = next((parent / "data" for parent in Path(__file__).resolve().parents if (parent / "docker-compose.yml").exists()), Path("/app/data"))


class HomeOpsService:
    def __init__(self, db_path: Path, executor: Any, notifier: Any | None = None, approval_ttl_seconds: int = 300, verification_attempts: int = 5, verification_interval_seconds: float = 2):
        self.db_path, self.executor, self.approval_ttl_seconds = db_path, executor, approval_ttl_seconds
        self.notifier = notifier
        self.verification_attempts, self.verification_interval_seconds = verification_attempts, verification_interval_seconds
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def diagnose_all(self) -> dict[str, Any]:
        executor_failure_reason = None
        try:
            diagnostics = self._mask(self.executor.all_diagnostics())
        except OSError as exc:
            diagnostics = None
            executor_failure_reason = self._executor_failure_reason(exc)
        summary = self._diagnosis_summary(diagnostics, executor_failure_reason)
        summary["operation_id"] = self._create_operation(summary)
        self._save_latest_summary(summary)
        return summary

    def restart_all(self, operation_id: str | None = None) -> dict[str, Any]:
        operation_id = self._operation_for_restart(operation_id) or self._create_manual_restart_operation()
        executor_failure_reason = None
        try:
            diagnostics = self._mask(self.executor.restart_all())
        except OSError as exc:
            if self._is_expected_restart_disconnect(exc):
                diagnostics = self._poll_all_diagnostics()
            else:
                diagnostics = None
                executor_failure_reason = self._executor_failure_reason(exc)
        if isinstance(diagnostics, dict) and diagnostics.get("status") == "accepted":
            summary = self._restart_pending_summary()
            self._mark_restart_pending(operation_id)
        else:
            summary = self._restart_summary(diagnostics, executor_failure_reason)
            self._complete_operation(operation_id, summary)
        summary["operation_id"] = operation_id
        self._save_latest_summary(summary)
        return summary

    def latest_summary(self) -> dict[str, Any] | None:
        self._complete_due_pending_restart()
        with self._connect() as conn:
            row = conn.execute("SELECT summary FROM latest_homeops_summary WHERE singleton_id=1").fetchone()
        return json.loads(row[0]) if row else None

    def operation_history(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT operation_id,status,created_at,diagnosed_at,restart_requested_at,verified_at,initial_result,final_result,restart_reason "
                "FROM homeops_operation_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            history = []
            for row in rows:
                events = conn.execute(
                    "SELECT event_type,occurred_at,payload FROM homeops_operation_events WHERE operation_id=? ORDER BY event_id",
                    (row[0],),
                ).fetchall()
                history.append(
                    {
                        "operation_id": row[0],
                        "status": row[1],
                        "created_at": row[2],
                        "diagnosed_at": row[3],
                        "restart_requested_at": row[4],
                        "verified_at": row[5],
                        "initial_result": json.loads(row[6]),
                        "final_result": json.loads(row[7]) if row[7] else None,
                        "restart_reason": json.loads(row[8]) if row[8] else [],
                        "status_label": {
                            "healthy": "점검 완료 · 정상",
                            "action_required": "점검 완료 · 조치 필요",
                            "restart_pending": "재시작 실행",
                            "recovered": "재점검 완료 · 복구 완료",
                            "failed": "재점검 완료 · 복구 실패",
                        }.get(row[1], row[1]),
                        "events": [
                            {"event_type": event[0], "occurred_at": event[1], "payload": json.loads(event[2])}
                            for event in events
                        ],
                    }
                )
        return history

    def create_diagnosis(self, service: str, record_healthy: bool = True) -> dict[str, Any]:
        self._require_service(service)
        diagnostics = self._mask(self.executor.diagnostics(service))
        unhealthy = self._needs_recovery(diagnostics)
        proposal = {"action": ALLOWED_ACTION if unhealthy else "no_action", "service": service, "requires_approval": bool(unhealthy),
                    "risk_level": "medium" if unhealthy else "low", "summary": "컨테이너 재시작 검토 필요" if unhealthy else "정상 상태: 조치 불필요", "evidence": diagnostics["logs"]}
        if not unhealthy and not record_healthy:
            return {"incident_id": None, "status": "healthy", "diagnostics": diagnostics, "proposal": proposal}
        incident_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute("INSERT INTO incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (incident_id, service, "proposed", self._now(), json.dumps(diagnostics), json.dumps(proposal), None, None))
        if unhealthy and self._consecutive_unhealthy(service) >= 3:
            if self._auto_restart_allowed(service):
                self.approve_incident(incident_id, "homeops-policy")
                self.execute_approved_incident(incident_id)
            else:
                self._notify("auto_restart_limit_reached", {"service": service, "reason": "최근 1시간 자동 재시작 2회 제한 도달"})
        return {"incident_id": incident_id, "status": "proposed", "diagnostics": diagnostics, "proposal": proposal}

    def _consecutive_unhealthy(self, service: str) -> int:
        with self._connect() as conn:
            rows = conn.execute("SELECT proposal FROM incidents WHERE service=? ORDER BY created_at DESC LIMIT 3", (service,)).fetchall()
            last_recovery = conn.execute("SELECT completed_at FROM incidents WHERE service=? AND status='verified' ORDER BY completed_at DESC LIMIT 1", (service,)).fetchone()
        if last_recovery and datetime.fromisoformat(last_recovery[0]) + timedelta(seconds=AUTO_RESTART_COOLDOWN_SECONDS) > datetime.now(timezone.utc):
            return 0
        return len(rows) if len(rows) == 3 and all(json.loads(row[0]).get("action") == ALLOWED_ACTION for row in rows) else 0

    def _auto_restart_allowed(self, service: str) -> bool:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with self._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM incidents WHERE service=? AND approved_by='homeops-policy' AND created_at>=? AND status IN ('executing', 'verified', 'failed')",
                (service, cutoff),
            ).fetchone()[0]
        return count < AUTO_RESTART_MAX_PER_HOUR

    @staticmethod
    def _needs_recovery(diagnostics: dict[str, Any]) -> bool:
        container = diagnostics.get("container", {})
        if container.get("status") != "running" or container.get("health") == "unhealthy":
            return True
        resource_pressure = (
            float(container.get("cpu_percent") or 0) >= CONTAINER_CPU_RESTART_PERCENT
            or float(container.get("memory_percent") or 0) >= CONTAINER_MEMORY_RESTART_PERCENT
        )
        return resource_pressure and any(_CRITICAL_LOG_PATTERN.search(str(line)) for line in diagnostics.get("logs", []))

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
        self._notify("container_restart_started", {"service": row[0], "reason": "연속 비정상 상태 또는 관리자 승인"})
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
        self._notify(
            "container_recovery_verified" if status == "verified" else "container_recovery_failed",
            {"service": row[0], "reason": "healthcheck 정상" if status == "verified" else "복구 검증 시간 내 healthcheck 정상화 실패"},
        )
        return {"status": status, "incident_id": incident_id}

    def observe_host_memory(self, memory_percent: Any) -> None:
        try:
            percent = float(memory_percent)
        except (TypeError, ValueError):
            return
        with self._connect() as conn:
            row = conn.execute("SELECT occurrences, active FROM alert_states WHERE alert_key='host_memory_high'").fetchone()
            occurrences, active = row if row else (0, 0)
            if percent >= HOST_MEMORY_ALERT_PERCENT:
                occurrences += 1
                if occurrences >= HOST_MEMORY_ALERT_CONSECUTIVE_SAMPLES and not active:
                    active = 1
                    self._notify("host_memory_high", {"memory_percent": percent, "reason": "90% 이상이 3회 연속 감지됨"})
            else:
                occurrences = 0
                if active:
                    active = 0
                    self._notify("host_memory_recovered", {"memory_percent": percent, "reason": "90% 미만으로 회복됨"})
            conn.execute(
                "INSERT INTO alert_states (alert_key, occurrences, active, updated_at) VALUES ('host_memory_high', ?, ?, ?) "
                "ON CONFLICT(alert_key) DO UPDATE SET occurrences=excluded.occurrences, active=excluded.active, updated_at=excluded.updated_at",
                (occurrences, active, self._now()),
            )

    def list_incidents(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT incident_id,service,status,created_at,proposal FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [{"incident_id": row[0], "service": row[1], "status": row[2], "created_at": row[3], "proposal": json.loads(row[4])} for row in rows]

    def _poll_all_diagnostics(self) -> list[dict[str, Any]] | None:
        last_diagnostics = None
        for attempt in range(self.verification_attempts):
            try:
                last_diagnostics = self._mask(self.executor.all_diagnostics())
                if self._all_services_healthy(last_diagnostics):
                    return last_diagnostics
            except HTTPError:
                return None
            except OSError:
                pass
            if attempt < self.verification_attempts - 1:
                time.sleep(self.verification_interval_seconds)
        return last_diagnostics

    @staticmethod
    def _is_expected_restart_disconnect(exc: OSError) -> bool:
        if isinstance(exc, HTTPError):
            return False
        cause = exc.reason if isinstance(exc, URLError) else exc
        return isinstance(cause, (ConnectionResetError, BrokenPipeError))

    def _all_services_healthy(self, diagnostics: list[dict[str, Any]]) -> bool:
        by_service = {str(item.get("service", "")): item for item in diagnostics}
        return all(service in by_service and self._unhealthy_reason(by_service[service]) is None for service in ALLOWED_SERVICES)

    def _diagnosis_summary(
        self,
        diagnostics: list[dict[str, Any]] | None,
        executor_failure_reason: str | None = None,
    ) -> dict[str, Any]:
        healthy: list[str] = []
        unhealthy: list[dict[str, str]] = []
        if diagnostics is None:
            reason = executor_failure_reason or "실행기 연결 실패"
            unhealthy = [{"service": service, "reason": reason} for service in sorted(ALLOWED_SERVICES)]
        else:
            for item in diagnostics:
                reason = self._unhealthy_reason(item)
                if reason:
                    unhealthy.append({"service": str(item.get("service", "")), "reason": reason})
                else:
                    healthy.append(str(item.get("service", "")))
        return {"kind": "diagnosis", "created_at": self._now(), "healthy": healthy, "unhealthy": unhealthy}

    def _restart_summary(
        self,
        diagnostics: list[dict[str, Any]] | None,
        executor_failure_reason: str | None = None,
    ) -> dict[str, Any]:
        recovered: list[str] = []
        failed: list[dict[str, str]] = []
        if diagnostics is None:
            reason = executor_failure_reason or "실행기 연결 실패"
            failed = [{"service": service, "reason": reason} for service in sorted(ALLOWED_SERVICES)]
        else:
            by_service = {str(item.get("service", "")): item for item in diagnostics}
            for service in sorted(ALLOWED_SERVICES):
                item = by_service.get(service)
                if item is None:
                    failed.append({"service": service, "reason": "실행기 응답 없음"})
                    continue
                reason = self._unhealthy_reason(item)
                if reason:
                    failed.append({"service": service, "reason": reason})
                else:
                    recovered.append(service)
        return {"kind": "restart", "created_at": self._now(), "healthy": [], "recovered": recovered, "failed": failed}

    def _restart_pending_summary(self) -> dict[str, Any]:
        return {"kind": "restart_pending", "created_at": self._now(), "healthy": [], "recovered": [], "failed": []}

    def _create_operation(self, summary: dict[str, Any]) -> str:
        operation_id = str(uuid.uuid4())
        status = "action_required" if summary.get("unhealthy") else "healthy"
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO homeops_operation_runs "
                "(operation_id,status,created_at,diagnosed_at,restart_requested_at,verified_at,initial_result,final_result,restart_reason) "
                "VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, ?)",
                (operation_id, status, now, now, json.dumps(summary), json.dumps(summary.get("unhealthy", []))),
            )
            self._append_operation_event(conn, operation_id, "diagnosis_completed", summary)
        return operation_id

    def _create_manual_restart_operation(self) -> str:
        operation_id = str(uuid.uuid4())
        now = self._now()
        initial_result = {"kind": "manual_restart", "healthy": [], "unhealthy": []}
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO homeops_operation_runs "
                "(operation_id,status,created_at,diagnosed_at,restart_requested_at,verified_at,initial_result,final_result,restart_reason) "
                "VALUES (?, 'restart_pending', ?, NULL, ?, NULL, ?, NULL, ?)",
                (operation_id, now, now, json.dumps(initial_result), json.dumps([])),
            )
        return operation_id

    def _operation_for_restart(self, requested_operation_id: str | None) -> str | None:
        with self._connect() as conn:
            if requested_operation_id:
                row = conn.execute(
                    "SELECT operation_id FROM homeops_operation_runs WHERE operation_id=? AND status='action_required'",
                    (requested_operation_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT operation_id FROM homeops_operation_runs WHERE status='action_required' ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
        return str(row[0]) if row else None

    def _mark_restart_pending(self, operation_id: str) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE homeops_operation_runs SET status='restart_pending', restart_requested_at=? WHERE operation_id=?",
                (now, operation_id),
            )
            self._append_operation_event(conn, operation_id, "restart_requested", {"operation_id": operation_id})

    def _complete_due_pending_restart(self) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT operation_id,restart_requested_at FROM homeops_operation_runs "
                "WHERE status='restart_pending' ORDER BY restart_requested_at DESC"
            ).fetchall()
        for row in rows:
            if not row[1] or not self._pending_restart_is_due(str(row[0]), row[1]):
                continue
            try:
                diagnostics = self._mask(self.executor.all_diagnostics())
            except OSError:
                continue
            summary = self._restart_summary(diagnostics)
            summary["operation_id"] = row[0]
            if summary["failed"] and not self._verification_attempt_limit_reached(str(row[0])):
                self._record_pending_verification(str(row[0]), summary)
                continue
            self._complete_operation(str(row[0]), summary)
            self._save_latest_summary(summary)

    def _pending_restart_is_due(self, operation_id: str, restart_requested_at: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT occurred_at FROM homeops_operation_events WHERE operation_id=? AND event_type='verification_pending' "
                "ORDER BY event_id DESC LIMIT 1",
                (operation_id,),
            ).fetchone()
        last_check = row[0] if row else restart_requested_at
        return datetime.fromisoformat(last_check) + timedelta(seconds=RESTART_VERIFICATION_DELAY_SECONDS) <= datetime.now(timezone.utc)

    def _verification_attempt_limit_reached(self, operation_id: str) -> bool:
        with self._connect() as conn:
            attempts = conn.execute(
                "SELECT COUNT(*) FROM homeops_operation_events WHERE operation_id=? AND event_type='verification_pending'",
                (operation_id,),
            ).fetchone()[0]
        return attempts >= 2

    def _record_pending_verification(self, operation_id: str, summary: dict[str, Any]) -> None:
        with self._connect() as conn:
            self._append_operation_event(conn, operation_id, "verification_pending", summary)

    def _complete_operation(self, operation_id: str, summary: dict[str, Any]) -> None:
        status = "recovered" if not summary.get("failed") else "failed"
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE homeops_operation_runs SET status=?, verified_at=?, final_result=? WHERE operation_id=?",
                (status, now, json.dumps(summary), operation_id),
            )
            self._append_operation_event(conn, operation_id, "verification_completed", summary)

    def _append_operation_event(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO homeops_operation_events (operation_id,event_type,occurred_at,payload) VALUES (?, ?, ?, ?)",
            (operation_id, event_type, self._now(), json.dumps(payload)),
        )

    @staticmethod
    def _unhealthy_reason(diagnostics: dict[str, Any]) -> str | None:
        if diagnostics.get("error") == "service_container_not_found":
            return "컨테이너를 찾을 수 없음"
        if diagnostics.get("error"):
            return "진단 수집 실패"
        container = diagnostics.get("container", {})
        if container.get("status") != "running":
            return "중지됨"
        if container.get("health") not in (None, "none", "healthy"):
            return "healthcheck 비정상"
        return None

    @staticmethod
    def _executor_failure_reason(exc: OSError) -> str:
        if isinstance(exc, HTTPError):
            if exc.code in (401, 403):
                return "실행기 인증 설정 확인 필요"
            return "실행기 내부 오류"
        return "실행기 연결 실패"

    def _save_latest_summary(self, summary: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO latest_homeops_summary (singleton_id, summary) VALUES (1, ?) "
                "ON CONFLICT(singleton_id) DO UPDATE SET summary=excluded.summary",
                (json.dumps(summary),),
            )

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS incidents (incident_id TEXT PRIMARY KEY, service TEXT, status TEXT, created_at TEXT, diagnostics TEXT, proposal TEXT, approved_by TEXT, completed_at TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS approval_tokens (incident_id TEXT PRIMARY KEY, token_hash TEXT, expires_at TEXT, consumed_at TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS alert_states (alert_key TEXT PRIMARY KEY, occurrences INTEGER NOT NULL, active INTEGER NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS latest_homeops_summary (singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1), summary TEXT NOT NULL)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS homeops_operation_runs ("
                "operation_id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, diagnosed_at TEXT, "
                "restart_requested_at TEXT, verified_at TEXT, initial_result TEXT NOT NULL, final_result TEXT, restart_reason TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS homeops_operation_events ("
                "event_id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, event_type TEXT NOT NULL, "
                "occurred_at TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_homeops_operation_runs_created_at ON homeops_operation_runs(created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_homeops_operation_events_operation_id ON homeops_operation_events(operation_id, event_id)")

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

    def _notify(self, event_type: str, details: dict[str, Any]) -> None:
        if self.notifier:
            notification_details = dict(details)
            notification_details.setdefault("admin_url", os.getenv("HOMEOPS_ADMIN_URL", "").strip())
            self.notifier.send(event_type, notification_details)


class ExecutorClient:
    def __init__(self):
        self.url = os.getenv("HOMEOPS_EXECUTOR_URL", "http://homeops-executor:8011").rstrip("/")
        self.secret = _executor_shared_secret()

    def diagnostics(self, service: str) -> dict[str, Any]:
        return self._request(f"/v1/diagnostics/{service}")

    def all_diagnostics(self) -> list[dict[str, Any]]:
        return self._request("/v1/diagnostics")

    def restart(self, incident_id: str, approval_token: str, service: str) -> dict[str, Any]:
        return self._request("/v1/restarts", {"incident_id": incident_id, "approval_token": approval_token, "action": ALLOWED_ACTION, "service": service})

    def restart_all(self) -> list[dict[str, Any]]:
        return self._request("/v1/restarts/all", method="POST")

    def health(self, service: str) -> bool:
        return bool(self.diagnostics(service).get("container", {}).get("health") == "healthy")

    def _request(self, path: str, payload: dict[str, Any] | None = None, method: str | None = None) -> Any:
        data = json.dumps(payload).encode() if payload else None
        request = Request(self.url + path, data=data, headers={"X-HomeOps-Executor-Secret": self.secret, "Content-Type": "application/json"}, method=method)
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode())


def get_homeops_service() -> HomeOpsService:
    from app.services.homeops_notifier import HomeOpsTelegramNotifier

    data_root = Path(os.getenv("HOMEOPS_DB_PATH", str(PROJECT_DATA_ROOT / "logs" / "homeops.sqlite3")))
    return HomeOpsService(data_root, ExecutorClient(), notifier=HomeOpsTelegramNotifier())


def _executor_shared_secret() -> str:
    return (
        os.getenv("HOMEOPS_EXECUTOR_SHARED_SECRET", "").strip()
        or os.getenv("ADMIN_STATUS_PASSWORD", "").strip()
    )
