"""Fail-closed tiered retention for verified encrypted PVC backups.

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
KST = timezone(timedelta(hours=9))


REMOTE_PARENT = "gdrive:PersonalServer-encrypted-backups"
SERVICES = {"portal": "portal", "book-memo": "book", "youtube-memo": "youtube", "crawler-worker": "crawler"}
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


def _proof(service: str, evidence: str, now: datetime, max_age_seconds: int = 86400) -> str:
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
    expiry_limit = (now if service == "portal" else completed) + timedelta(seconds=max_age_seconds)
    if not (now - timedelta(days=1) <= completed <= restored <= now < expires <= expiry_limit):
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
    return "portal-" + filename if service == "portal" else filename


def _inventory(raw: str, now: datetime, *, portal: bool = False) -> list[tuple[datetime, str, int, str]]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise RetentionError("retention remote inventory invalid") from error
    if not isinstance(parsed, list) or not parsed:
        raise RetentionError("retention remote inventory invalid")
    if portal:
        parsed = [item for item in parsed if not isinstance(item, dict) or
                  not isinstance(item.get("Name"), str) or item["Name"].startswith("portal-")]
        if not parsed:
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
        match = ARCHIVE_NAME.fullmatch(name.removeprefix("portal-") if portal else name)
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
        object_id = item.get("ID")
        if not isinstance(object_id, str) or not object_id:
            raise RetentionError("retention remote identity unavailable")
        result.append((created, name, item["Size"], json.dumps([modified, object_id])))
    return sorted(result, reverse=True)


def _list(remote: str, rclone_argv: tuple[str, ...], command: Callable[..., str], now: datetime, *, portal: bool = False) -> list[tuple[datetime, str, int, str]]:
    try:
        raw = command(*rclone_argv, "lsjson", remote, timeout=120)
    except Exception as error:
        raise RetentionError("retention remote listing failed") from error
    return _inventory(raw, now, portal=portal)


def retained_names(inventory: list[tuple[datetime, str, int, str]], now: datetime, *, minimum: int = 7) -> set[str]:
    """KST daily 7; preceding four 7-day bands; older previous 3 calendar months.

    The newest archive in each occupied bucket is kept. The newest seven
    archives are a safety floor when history is sparse or runs are repeated.
    """
    today = now.astimezone(KST).date()
    selected = {item[1] for item in inventory[:minimum]}
    buckets: set[tuple[str, int]] = set()
    for created, filename, _, _ in inventory:
        day = created.astimezone(KST).date()
        age = (today - day).days
        month_age = (today.year - day.year) * 12 + today.month - day.month
        if 0 <= age < 7:
            bucket = ("day", age)
        elif 7 <= age < 35:
            bucket = ("week", (age - 7) // 7)
        elif age >= 35 and 1 <= month_age <= 3:
            bucket = ("month", month_age)
        else:
            continue
        if bucket not in buckets:
            buckets.add(bucket)
            selected.add(filename)
    return selected


def run_retention(
    service: str, evidence: str, rclone_argv: tuple[str, ...],
    command: Callable[..., str], *, delete: bool = False, now: datetime | None = None, remote_root: str = REMOTE_PARENT, report: dict | None = None, max_age_seconds: int = 86400,
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
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 31536000:
        raise RetentionError("retention age invalid")
    protected = _proof(service, evidence, now, max_age_seconds)
    if not isinstance(remote_root, str) or not remote_root or "\n" in remote_root:
        raise RetentionError("retention remote invalid")
    remote = remote_root if service == "portal" else f"{REMOTE_PARENT}/{service}"
    inventory = _list(remote, rclone_argv, command, now, portal=service == "portal")
    if inventory[0][1] != protected:
        raise RetentionError("retention evidence does not match latest archive")
    retained = retained_names(inventory, now) | {protected}
    candidates = [item[1] for item in reversed(inventory) if item[1] not in retained]
    if report is not None:
        report.update(total_count=len(inventory), retained_count=len(retained), delete_count=len(candidates),
                      total_bytes=sum(item[2] for item in inventory),
                      retained_bytes=sum(item[2] for item in inventory if item[1] in retained),
                      delete_bytes=sum(item[2] for item in inventory if item[1] not in retained))
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
        expected_latest = _proof(service, evidence, check_now, max_age_seconds)
        current = _list(remote, rclone_argv, command, check_now, portal=service == "portal")
        if current != inventory or current[0][1] != expected_latest:
            raise RetentionError("retention remote inventory changed")
        try:
            command(*rclone_argv, "--drive-use-trash=false", "deletefile",
                    f"{remote}/{filename}", timeout=120)
        except Exception as error:
            raise RetentionError("retention remote deletion failed") from error
        inventory = [item for item in inventory if item[1] != filename]
    return candidates


def main() -> int:
    # Portal shell holds its lock and supplies only credential file arguments.
    import argparse
    import subprocess
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument("--portal-evidence", "--evidence", dest="evidence", required=True)
    parser.add_argument("--service", choices=tuple(SERVICES), default="portal")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--max-age-seconds", type=int, default=86400)
    parser.add_argument("rclone_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.rclone_args
    if argv[:1] == ["--"]:
        argv = argv[1:]
    def command(*command_argv, timeout):
        return subprocess.run(command_argv, check=True, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=timeout).stdout
    try:
        report = {}
        candidates = run_retention(args.service, Path(args.evidence).read_text(),
                                   ("rclone", *argv), command, delete=args.delete, remote_root=args.remote, report=report, max_age_seconds=args.max_age_seconds)
        print(json.dumps(report, sort_keys=True))
        return 0
    except (RetentionError, OSError, ValueError):
        print("pvc_backup_retention=FAIL")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
