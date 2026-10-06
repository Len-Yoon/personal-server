"""Atomic retry and optimistic-edit checks shared by the service write paths."""
import hashlib
import json
import sqlite3
from uuid import UUID


class WriteConflict(ValueError):
    pass


def init_write_requests(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS write_requests (
            request_id TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            result_id INTEGER NOT NULL
        )
    """)


def request_key(value: str) -> str:
    if not value:
        return ""
    try:
        return UUID(value).hex
    except (ValueError, AttributeError) as error:
        raise ValueError("저장 요청 식별자가 올바르지 않습니다.") from error


def payload_hash(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def replay_result(connection, request_id: str, operation: str, payload: object) -> int | None:
    key = request_key(request_id)
    if not key:
        return None
    row = connection.execute("SELECT * FROM write_requests WHERE request_id = ?", (key,)).fetchone()
    if not row:
        return None
    if row["operation"] != operation or row["payload_hash"] != payload_hash(payload):
        raise WriteConflict("같은 저장 요청의 내용이 변경되었습니다. 입력을 확인하고 새 요청으로 저장해주세요.")
    return row["result_id"]


def record_result(connection, request_id: str, operation: str, payload: object, result_id: int) -> None:
    key = request_key(request_id)
    if key:
        connection.execute(
            "INSERT INTO write_requests (request_id, operation, payload_hash, result_id) VALUES (?, ?, ?, ?)",
            (key, operation, payload_hash(payload), result_id),
        )


def check_version(row, expected_version: int | None) -> None:
    if expected_version is not None and (
        isinstance(expected_version, bool) or not isinstance(expected_version, int)
        or expected_version < 1 or row["version"] != expected_version
    ):
        raise WriteConflict("다른 화면에서 수정된 기록입니다. 입력을 복사하고 새로고침해 최신 기록을 확인해주세요.")
