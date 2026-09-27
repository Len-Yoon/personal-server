"""Fail-closed retention for the three verified non-Portal PVC backups.

The caller must hold the service's backup lock for deletion and pass the
current completed ConfigMap evidence. Preview does not acquire a lock.
Deletion is an explicit choice.
No credentials or remote diagnostics are included in raised errors.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Callable


REMOTE_PARENT = "gdrive:PersonalServer-encrypted-backups"
SERVICES = {"book-memo": "book", "youtube-memo": "youtube", "crawler-worker": "crawler"}
ARCHIVE_NAME = re.compile(r"(\d{8}T\d{6}Z)-([1-9]\d*)\.tar\.age\Z", re.ASCII)
TIME = "%Y-%m-%dT%H:%M:%SZ"
RUN_TIME = "%Y%m%dT%H%M%SZ"


class RetentionError(RuntimeError):
    """A fixed-label retention failure; never includes command output."""


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: str, fmt: str) -> datetime:
    try:
        parsed = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        if parsed.strftime(fmt) != value:
            raise ValueError("noncanonical timestamp")
        return parsed
    except (TypeError, ValueError) as error:
        raise RetentionError("retention timestamp invalid") from error


def _proof(service: str, evidence: str, now: datetime) -> str:
    if not isinstance(evidence, str) or not evidence:
        raise RetentionError("retention evidence unavailable")
    values: dict[str, str] = {}
    for line in evidence.splitlines():
        if not line or "=" not in line:
            raise RetentionError("retention evidence invalid")
        key, value = line.split("=", 1)
        if not key or key in values or not value:
            raise RetentionError("retention evidence invalid")
        values[key] = value
    expected = {
        "schema_version": "1", "scope": service, "backup_status": "success",
        "encrypted": "true", "restore_status": "success",
        "source_runtime": "k3s-pvc", "restore_path_check": "success",
    }
    if any(values.get(key) != value for key, value in expected.items()):
        raise RetentionError("retention evidence invalid")
    completed = _utc(values.get("backup_completed_at"), TIME)
    restored = _utc(values.get("restore_verified_at"), TIME)
    expires = _utc(values.get("evidence_expires_at"), TIME)
    if not (now - timedelta(days=1) <= completed <= restored <= now < expires <= completed + timedelta(days=1)):
        raise RetentionError("retention evidence expired")
    if any(re.fullmatch(r"sha256:[0-9a-f]{64}", values.get(key, "")) is None
           for key in ("artifact_digest", "source_digest")):
        raise RetentionError("retention evidence invalid")
    prefix = SERVICES[service] + "-"
    backup_id = values.get("backup_id", "")
    if not backup_id.startswith(prefix):
        raise RetentionError("retention evidence invalid")
    filename = backup_id.removeprefix(prefix) + ".tar.age"
    match = ARCHIVE_NAME.fullmatch(filename)
    if match is None or not (_utc(match.group(1), RUN_TIME) <= completed):
        raise RetentionError("retention evidence invalid")
    return filename


def _inventory(raw: str, now: datetime) -> list[tuple[datetime, str, int, str]]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise RetentionError("retention remote inventory invalid") from error
    if not isinstance(parsed, list) or not parsed:
        raise RetentionError("retention remote inventory invalid")
    seen: set[str] = set()
    result = []
    for item in parsed:
        if not isinstance(item, dict):
            raise RetentionError("retention remote item invalid")
        name = item.get("Name")
        if not isinstance(name, str) or name in seen or item.get("Path") != name or item.get("IsDir") is not False:
            raise RetentionError("retention remote item invalid")
        seen.add(name)
        match = ARCHIVE_NAME.fullmatch(name)
        if match is None or type(item.get("Size")) is not int or item["Size"] <= 0:
            raise RetentionError("retention remote item invalid")
        created = _utc(match.group(1), RUN_TIME)
        modified = item.get("ModTime")
        if not isinstance(modified, str):
            raise RetentionError("retention remote item invalid")
        try:
            modification = datetime.fromisoformat(modified.replace("Z", "+00:00"))
        except ValueError as error:
            raise RetentionError("retention remote item invalid") from error
        if modification.tzinfo is None or created > now or modification > now:
            raise RetentionError("retention future remote item")
        result.append((created, name, item["Size"], modified))
    return sorted(result, reverse=True)


def _list(remote: str, rclone_argv: tuple[str, ...], command: Callable[..., str], now: datetime) -> list[tuple[datetime, str, int, str]]:
    try:
        raw = command(*rclone_argv, "lsjson", remote, timeout=120)
    except Exception as error:
        raise RetentionError("retention remote listing failed") from error
    return _inventory(raw, now)


def run_retention(
    service: str, evidence: str, rclone_argv: tuple[str, ...],
    command: Callable[..., str], *, delete: bool = False, now: datetime | None = None,
) -> list[str]:
    """Return deletion candidates; delete them only when ``delete=True``.

    The caller must pass its fixed service identifier, current ConfigMap
    evidence, rclone credentials argv, and existing ``run_command`` callable.
    A changed inventory before any delete aborts without continuing.
    """
    if (not isinstance(service, str) or service not in SERVICES or
            not isinstance(rclone_argv, tuple) or not rclone_argv or type(delete) is not bool):
        raise RetentionError("retention arguments invalid")
    now = now or _clock()
    if now.tzinfo is None:
        raise RetentionError("retention clock invalid")
    now = now.astimezone(timezone.utc)
    protected = _proof(service, evidence, now)
    remote = f"{REMOTE_PARENT}/{service}"
    inventory = _list(remote, rclone_argv, command, now)
    if inventory[0][1] != protected:
        raise RetentionError("retention evidence does not match latest archive")
    threshold = now - timedelta(days=30)
    candidates = [item[1] for item in reversed(inventory[7:]) if item[0] < threshold and item[1] != protected]
    if not delete:
        return candidates
    for filename in candidates:
        # A fresh inventory before each irreversible operation catches most
        # concurrent Drive changes, including duplicate names in My Drive.
        # The evidence age and future-item checks use the current clock too.
        check_now = _clock()
        if check_now.tzinfo is None:
            raise RetentionError("retention clock invalid")
        check_now = check_now.astimezone(timezone.utc)
        expected_latest = _proof(service, evidence, check_now)
        current = _list(remote, rclone_argv, command, check_now)
        if current != inventory or current[0][1] != expected_latest:
            raise RetentionError("retention remote inventory changed")
        try:
            command(*rclone_argv, "--drive-use-trash=false", "deletefile",
                    f"{remote}/{filename}", timeout=120)
        except Exception as error:
            raise RetentionError("retention remote deletion failed") from error
        inventory = [item for item in inventory if item[1] != filename]
    return candidates
