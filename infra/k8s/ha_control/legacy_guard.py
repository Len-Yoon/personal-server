"""Strict composition of fresh, source-bound Windows and Linux observations.

This module never stops a workload or grants authority from a saved snapshot.
The approved inventory is a comparison target; live provider completeness and
the frozen states of registered recreation clients are independently required.
"""
import base64
from datetime import datetime, timezone
import hashlib
import json
import re
import sys

LIMIT = 1048576
HEX = re.compile(r"[0-9a-f]{64}\Z")


class GuardBlocked(RuntimeError):
    pass


def deny():
    raise GuardBlocked("legacy_writer_guard_unverified")


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        deny()


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        deny()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def hex64(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        deny()


def unique_ids(value):
    if not isinstance(value, list):
        deny()
    for item in value:
        hex64(item)
    if len(value) != len(set(value)):
        deny()


def validate_plan(value):
    exact(value, ("schema_version", "windows", "linux"))
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        deny()
    windows = value["windows"]
    exact(windows, ("schema_version", "boot_ticks", "principal_sha256", "inventory_sha256",
                    "frozen_services", "frozen_tasks", "provider_services", "provider_tasks", "provider_processes"))
    if (type(windows["schema_version"]) is not int or windows["schema_version"] != 1
            or not isinstance(windows["boot_ticks"], str)
            or not re.fullmatch(r"[1-9][0-9]{1,20}", windows["boot_ticks"])):
        deny()
    for field in ("principal_sha256", "inventory_sha256"):
        hex64(windows[field])
    for field in ("frozen_services", "frozen_tasks", "provider_services", "provider_tasks", "provider_processes"):
        unique_ids(windows[field])
    if (set(windows["frozen_services"]) & set(windows["provider_services"])
            or set(windows["frozen_tasks"]) & set(windows["provider_tasks"])):
        deny()
    linux = value["linux"]
    exact(linux, ("distribution", "reader_user", "spec"))
    if (not isinstance(linux["distribution"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", linux["distribution"])
            or linux["reader_user"] not in ("default", "root")
            or not isinstance(linux["spec"], dict)):
        deny()
    # The self-contained Linux reader owns the complete Linux schema. These
    # identity fields are mandatory here too; no privilege fallback is inferred.
    integer(linux["spec"].get("uid"))
    if linux["reader_user"] == "root" and linux["spec"]["uid"] != 0:
        deny()
    return value


def validate_windows(spec, observation):
    exact(observation, ("schema_version", "complete", "boot_ticks", "principal_sha256",
                        "services", "tasks", "processes", "startup_sha256"))
    if (type(observation["schema_version"]) is not int or observation["schema_version"] != 1
            or observation["complete"] is not True
            or observation["boot_ticks"] != spec["boot_ticks"]
            or observation["principal_sha256"] != spec["principal_sha256"]):
        deny()
    hex64(observation["startup_sha256"])
    service_ids, task_ids = set(), set()
    for category, frozen, seen in (("services", spec["frozen_services"], service_ids),
                                   ("tasks", spec["frozen_tasks"], task_ids)):
        rows = observation[category]
        if not isinstance(rows, list):
            deny()
        for row in rows:
            keys = ("id", "spec_sha256", "creator", "provider", "disabled", "stopped") if category == "services" else (
                "id", "spec_sha256", "creator", "provider", "disabled")
            exact(row, keys)
            for field in ("id", "spec_sha256"):
                hex64(row[field])
            if row["id"] in seen:
                deny()
            seen.add(row["id"])
            for field in set(keys) - {"id", "spec_sha256"}:
                if type(row[field]) is not bool:
                    deny()
            if row["creator"] and row["id"] not in frozen:
                deny()
            if row["id"] not in frozen and (not row["provider"] or row["id"] not in spec["provider_" + category]):
                deny()
            if row["id"] in frozen and (not row["disabled"] or category == "services" and not row["stopped"]):
                deny()
        if not set(frozen) <= seen:
            deny()
        if not set(spec["provider_" + category]) <= seen:
            deny()
    rows = observation["processes"]
    if not isinstance(rows, list):
        deny()
    seen = set()
    for row in rows:
        exact(row, ("id", "spec_sha256", "creator", "provider"))
        hex64(row["id"])
        hex64(row["spec_sha256"])
        if (row["id"] in seen or type(row["creator"]) is not bool or row["creator"]
                or row["provider"] is not True or row["id"] not in spec["provider_processes"]):
            deny()
        seen.add(row["id"])
    if seen != set(spec["provider_processes"]):
        deny()
    if digest(observation) != spec["inventory_sha256"]:
        deny()


def verify(request):
    exact(request, ("schema_version", "nonce", "plan_sha256", "cluster", "generation",
                    "collector_sha256", "guard", "windows", "linux_receipt", "plan_raw", "approval_raw"))
    if type(request["schema_version"]) is not int or request["schema_version"] != 1:
        deny()
    for field in ("nonce", "plan_sha256", "collector_sha256"):
        hex64(request[field])
    integer(request["generation"], 1)
    if not isinstance(request["cluster"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request["cluster"]):
        deny()
    try:
        raw = base64.b64decode(request["plan_raw"], validate=True)
        approval_raw = base64.b64decode(request["approval_raw"], validate=True)
        if len(raw) > LIMIT or len(approval_raw) > LIMIT:
            deny()
        plan = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: deny())
        approval = json.loads(approval_raw, object_pairs_hook=pairs, parse_constant=lambda _: deny())
        exact(approval, ("action", "source_commit", "plan_sha256", "expires_at"))
        hex64(plan["legacy_guard_code_sha256"])
        hex64(globals().get("__collector_sha256__"))
        hex64(plan["legacy_linux_sha256"])
        if not isinstance(plan["source_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", plan["source_commit"]):
            deny()
        if (hashlib.sha256(raw).hexdigest() != request["plan_sha256"]
                or plan["legacy_guard"] != request["guard"]
                or plan["cluster"] != request["cluster"]
                or type(plan["generation"]) is not int or plan["generation"] != request["generation"]
                or plan["legacy_linux_sha256"] != request["collector_sha256"]
                or plan["legacy_guard_code_sha256"] != globals().get("__collector_sha256__")
                or approval["plan_sha256"] != request["plan_sha256"]
                or approval["source_commit"] != plan["source_commit"]
                or approval["action"] not in ("fence", "ha-transition")
                or not isinstance(approval["expires_at"], str)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,7})?Z", approval["expires_at"])):
            deny()
        expires = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
        remaining = (expires - datetime.now(timezone.utc)).total_seconds()
        if not 0 < remaining <= 86400:
            deny()
    except (ValueError, TypeError, KeyError):
        deny()
    guard = validate_plan(request["guard"])
    validate_windows(guard["windows"], request["windows"])
    fields = ("schema_version", "nonce", "plan_sha256", "cluster", "generation", "collector_sha256",
              "complete", "verified")
    receipt = request["linux_receipt"]
    exact(receipt, fields)
    if any(receipt[k] != request[k] or type(receipt[k]) is not type(request[k]) for k in fields[:6]):
        deny()
    if receipt["complete"] is not True or receipt["verified"] is not True:
        deny()
    return {**{key: request[key] for key in fields[:6]}, "complete": True, "verified": True}


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            deny()
        result[key] = value
    return result


def main():
    try:
        raw = sys.stdin.buffer.read(LIMIT + 1)
        if len(raw) > LIMIT:
            deny()
        request = json.loads(raw, object_pairs_hook=pairs,
                             parse_constant=lambda _: deny())
        print(json.dumps(verify(request), sort_keys=True))
    except (GuardBlocked, OSError, ValueError, TypeError, KeyError, RecursionError):
        print('{"status":"blocked"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
