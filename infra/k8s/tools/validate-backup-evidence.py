#!/usr/bin/env python3
"""Fail-closed validation for a Portal encrypted-backup evidence record."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "scope",
        "backup_status",
        "encrypted",
        "backup_completed_at",
        "restore_status",
        "restore_verified_at",
        "evidence_expires_at",
        "backup_id",
        "source_runtime",
    }
)
OPTIONAL_KEYS = frozenset({"artifact_digest", "source_digest", "restore_check", "restore_path_check"})
ALLOWED_KEYS = REQUIRED_KEYS | OPTIONAL_KEYS
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
BACKUP_ID_RE = re.compile(r"[A-Za-z0-9._-]+\Z")
ARTIFACT_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
SOURCE_RUNTIME_VALUES = frozenset({"compose-local", "k3s-pvc"})


class EvidenceError(ValueError):
    """Raised when evidence cannot prove a safe Portal backup."""


def parse_timestamp(value: str, key: str) -> datetime:
    try:
        return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise EvidenceError(f"{key} must be a UTC ISO-8601 timestamp ending in Z") from error


def parse_evidence(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise EvidenceError("evidence file cannot be read") from error

    values: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#") or "=" not in line:
            raise EvidenceError("evidence contains a blank, comment, or malformed line")
        key, value = line.split("=", 1)
        if not key or not value or key.strip() != key or value.strip() != value:
            raise EvidenceError("evidence contains a blank key/value or surrounding whitespace")
        if key not in ALLOWED_KEYS:
            raise EvidenceError("evidence contains an unknown key")
        if key in values:
            raise EvidenceError("evidence contains a duplicate key")
        values[key] = value

    missing = REQUIRED_KEYS - values.keys()
    if missing:
        raise EvidenceError("evidence is missing required keys")
    return values


def validate_evidence(values: dict[str, str], now: datetime, max_age_seconds: int) -> None:
    expected = {
        "schema_version": "1",
        "scope": "portal",
        "backup_status": "success",
        "encrypted": "true",
        "restore_status": "success",
    }
    for key, value in expected.items():
        if values[key] != value:
            raise EvidenceError(f"{key} is not an approved value")

    if not BACKUP_ID_RE.fullmatch(values["backup_id"]):
        raise EvidenceError("backup_id is not an opaque safe identifier")
    if values["source_runtime"] not in SOURCE_RUNTIME_VALUES:
        raise EvidenceError("source_runtime is not approved")
    if "artifact_digest" in values and not ARTIFACT_DIGEST_RE.fullmatch(values["artifact_digest"]):
        raise EvidenceError("artifact_digest must be a lowercase sha256 digest")
    if "source_digest" in values and not ARTIFACT_DIGEST_RE.fullmatch(values["source_digest"]):
        raise EvidenceError("source_digest must be a lowercase sha256 digest")
    if values.get("restore_check", "sqlite_quick_check") != "sqlite_quick_check":
        raise EvidenceError("restore_check is not approved")
    if values.get("restore_path_check", "success") != "success":
        raise EvidenceError("restore_path_check is not approved")

    backup_time = parse_timestamp(values["backup_completed_at"], "backup_completed_at")
    restore_time = parse_timestamp(values["restore_verified_at"], "restore_verified_at")
    expiry_time = parse_timestamp(values["evidence_expires_at"], "evidence_expires_at")
    if restore_time < backup_time:
        raise EvidenceError("restore verification predates the backup it must prove")
    if backup_time > now or restore_time > now:
        raise EvidenceError("backup or restore timestamp is in the future")
    if expiry_time <= now:
        raise EvidenceError("evidence has expired")
    max_age = max_age_seconds
    if (now - backup_time).total_seconds() > max_age or (now - restore_time).total_seconds() > max_age:
        raise EvidenceError("backup or restore verification is too old")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--max-age-seconds", type=int, default=86400)
    parser.add_argument("--now", help="UTC Z timestamp; test-only deterministic clock")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_age_seconds < 0:
        print("backup_evidence=FAIL", file=sys.stderr)
        return 1
    try:
        now = parse_timestamp(args.now, "now") if args.now else datetime.now(timezone.utc).replace(microsecond=0)
        values = parse_evidence(args.evidence)
        validate_evidence(values, now, args.max_age_seconds)
    except EvidenceError:
        print("backup_evidence=FAIL", file=sys.stderr)
        return 1
    print("backup_evidence=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
