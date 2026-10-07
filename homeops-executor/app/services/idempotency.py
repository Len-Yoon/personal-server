"""Durable restart claims. An uncertain side effect is never retried automatically."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any


class ClaimError(Exception):
    def __init__(self, detail: str, status_code: int = 409):
        self.detail = detail
        self.status_code = status_code


def state_path() -> Path:
    return Path(os.getenv("HOMEOPS_IDEMPOTENCY_DB_PATH", "/data/homeops-executor/restarts.sqlite3"))


def payload_hash(payload: dict[str, Any]) -> str:
    # Only the digest is persisted; approval tokens and shared secrets are not stored.
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


class RestartStore:
    def __init__(self, path: Path | None = None, ttl: int = 86400 * 7, capacity: int = 10000):
        self.path = path or state_path()
        self.ttl = ttl
        self.capacity = capacity

    @contextmanager
    def _connect(self):
        if not self.path.is_absolute():
            raise ClaimError("restart_state_unavailable", 503)
        # The deployment must provision a writable, persistent directory explicitly.
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("CREATE TABLE IF NOT EXISTS restart_claims (request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, created_at REAL NOT NULL, state TEXT NOT NULL, result TEXT)")
            with connection:
                yield connection
        finally:
            connection.close()

    def claim(self, request_id: str, digest: str) -> dict[str, Any] | None:
        rejection = None
        result = None
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                # TTL releases response bodies, never the durable request ID.
                # Expired IDs remain tombstones; capacity exhaustion fails closed.
                connection.execute("UPDATE restart_claims SET state='expired', result=NULL WHERE state='completed' AND created_at<?", (time.time() - self.ttl,))
                row = connection.execute("SELECT payload_hash, state, result FROM restart_claims WHERE request_id=?", (request_id,)).fetchone()
                if row:
                    if row[0] != digest:
                        rejection = ClaimError("restart_request_conflict")
                    elif row[1] == "expired":
                        rejection = ClaimError("restart_request_expired")
                    elif row[1] != "completed":
                        rejection = ClaimError("restart_outcome_unknown")
                    else:
                        result = json.loads(row[2])
                        if not isinstance(result, dict):
                            rejection = ClaimError("restart_state_unavailable", 503)
                else:
                    count = connection.execute("SELECT COUNT(*) FROM restart_claims").fetchone()[0]
                    if count >= self.capacity:
                        rejection = ClaimError("restart_state_capacity", 503)
                    else:
                        connection.execute("INSERT INTO restart_claims VALUES (?, ?, ?, 'inflight', NULL)", (request_id, digest, time.time()))
            # Commit tombstones even when the requested operation is rejected.
            if rejection:
                raise rejection
            return result
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            raise ClaimError("restart_state_unavailable", 503) from exc

    def complete(self, request_id: str, result: dict[str, Any]) -> None:
        try:
            with self._connect() as connection:
                updated = connection.execute("UPDATE restart_claims SET state='completed', result=? WHERE request_id=? AND state='inflight'", (json.dumps(result, sort_keys=True), request_id)).rowcount
                if updated != 1:
                    raise ClaimError("restart_outcome_unknown")
        except (sqlite3.Error, OSError) as exc:
            raise ClaimError("restart_state_unavailable", 503) from exc

    def ready(self) -> bool:
        """Read-only local check; never create a database as a readiness probe."""
        try:
            if not self.path.is_absolute() or not self.path.parent.is_dir():
                return False
            if not os.access(self.path.parent, os.W_OK | os.X_OK):
                return False
            if not self.path.exists():
                return True
            if not self.path.is_file() or not os.access(self.path, os.R_OK | os.W_OK):
                return False
            connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=1)
            try:
                count = connection.execute("SELECT COUNT(*) FROM restart_claims").fetchone()[0]
                valid_states = connection.execute("SELECT COUNT(*) FROM restart_claims WHERE state NOT IN ('inflight', 'completed', 'expired')").fetchone()[0]
            finally:
                connection.close()
            return count < self.capacity and valid_states == 0
        except (sqlite3.Error, OSError, ValueError):
            return False
