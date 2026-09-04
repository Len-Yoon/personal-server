#!/usr/bin/env python3
"""Fail-closed validation of the transition runner's declarative policy."""
import json
import re
import sys
from pathlib import Path

SERVICES = ("crawler-worker", "youtube-memo", "book-memo")
PHASES = ("preflight", "backup", "stop-compose", "copy-pvc", "start-k3s", "verify-private", "record")
TOP_KEYS = {"schema_version", "namespace", "services", "phases", "timeouts"}
SERVICE_KEYS = {"name", "pvc", "image"}
DIGEST = re.compile(r"^[^@/\s]+(?:/[^@/\s]+)*@sha256:[0-9a-f]{64}$")
SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
EXPECTED_PVCS = dict(zip(SERVICES, ("crawler-worker-data", "youtube-memo-data", "book-memo-data")))
EXPECTED_IMAGE_PREFIXES = {service: f"ghcr.io/personal-server/{service}@sha256:" for service in SERVICES}


def fail(message):
    print("transition_runner_policy=FAIL", file=sys.stderr)
    return 1


def validate(data):
    if not isinstance(data, dict) or set(data) != TOP_KEYS:
        raise ValueError("unknown or missing policy key")
    if data["schema_version"] != 1 or data["namespace"] != "personal-server":
        raise ValueError("unsupported schema or namespace")
    if data["phases"] != list(PHASES):
        raise ValueError("lifecycle phases are not fixed")
    if not isinstance(data["services"], list) or len(data["services"]) != 3:
        raise ValueError("services must contain exactly three entries")
    names = []
    for service in data["services"]:
        if not isinstance(service, dict) or set(service) != SERVICE_KEYS:
            raise ValueError("unknown service key")
        name, pvc, image = service["name"], service["pvc"], service["image"]
        if name in names or name != SERVICES[len(names)]:
            raise ValueError("services are duplicated or out of order")
        if not isinstance(pvc, str) or pvc != EXPECTED_PVCS[name] or not SAFE_NAME.fullmatch(pvc):
            raise ValueError("unsafe PVC name")
        if (not isinstance(image, str) or not DIGEST.fullmatch(image)
                or not image.startswith(EXPECTED_IMAGE_PREFIXES[name])):
            raise ValueError("image must be immutable lowercase sha256 digest")
        names.append(name)
    if not isinstance(data["timeouts"], dict) or set(data["timeouts"]) != set(PHASES):
        raise ValueError("timeouts do not match phases")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in data["timeouts"].values()):
        raise ValueError("timeouts must be positive integers")


def main():
    if len(sys.argv) != 2:
        return fail("exactly one policy path is required")
    try:
        path = Path(sys.argv[1])
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        validate(data)
    except (OSError, ValueError, json.JSONDecodeError):
        return fail("invalid transition runner policy")
    print("transition_runner_policy=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
