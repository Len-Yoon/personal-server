#!/usr/bin/env python3
"""Classify changed paths for the narrowly scoped N100 safe deployment lane."""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from typing import Iterable, Literal, Sequence


SAFE_SERVICES = (
    "crawler-worker",
    "youtube-memo",
    "book-memo",
    "car-care-worker",
)
SAFE_SERVICE_PREFIXES = {
    "crawler-worker/": "crawler-worker",
    "youtube-memo/": "youtube-memo",
    "book-memo/": "book-memo",
    "car-care-worker/": "car-care-worker",
}
SAFE_LOG_MOUNTPOINT_MARKERS = frozenset(
    f"{service}/app/data/logs/.gitkeep"
    for service in ("crawler-worker", "youtube-memo", "book-memo")
)
BLOCKED_PREFIXES = (
    "portal-web/",
    "caddy/",
    "system-agent/",
    "homeops-executor/",
    "infra/k8s/",
)
SHARED_COMPOSE_PATHS = {"docker-compose.yml", "docker-compose.n100.yml"}
SAFE_NON_RUNTIME_PATHS = {
    "scripts/classify-n100-safe-deployment.py",
    "scripts/record_token_measurement.py",
    "scripts/verify_change_scope.py",
}
REASON_NO_CHANGES = "no_changed_paths"
REASON_CONTROL_CHARACTER = "blocked_control_character"
REASON_BLOCKED_PATH = "blocked_path"
REASON_UNKNOWN_RUNTIME = "blocked_unknown_runtime"
REASON_SAFE_SERVICE = "safe_service_change"
REASON_DOCUMENTATION = "documentation_or_test_only"

DeploymentAction = Literal["skip", "deploy", "blocked"]


@dataclass(frozen=True)
class DeploymentDecision:
    action: DeploymentAction
    services: tuple[str, ...]
    reason: str


def _normalise_path(path: str) -> str:
    return path.replace("\\", "/")


def _has_traversal_component(path: str) -> bool:
    return any(component in {".", ".."} for component in path.split("/"))


def _is_sensitive_service_path(path: str) -> bool:
    parts = path.split("/")
    components = [part.lower() for part in parts]
    name = components[-1]
    return (
        any(
            any(signal in part for signal in ("credential", "secret", "private", "password", "token", "key"))
            or part.startswith(".env")
            or part in {"data", "runtime", "state", ".state", "storage", "cache", "var", "config"}
            for part in components[1:]
        )
        or name.startswith(".env")
        or name.endswith(".env")
        or name.endswith((".sqlite", ".sqlite3", ".db", ".key", ".pem"))
    )


def _is_allowed_service_path(path: str) -> bool:
    if path in SAFE_LOG_MOUNTPOINT_MARKERS:
        return True
    for prefix in SAFE_SERVICE_PREFIXES:
        if path.startswith(prefix):
            relative = path[len(prefix) :]
            if relative in {"Dockerfile", "requirements.txt"}:
                return True
            if relative.startswith("app/"):
                return (
                    relative.endswith(".py")
                    or (relative.startswith("app/templates/") and relative.endswith(".html"))
                    or (relative.startswith("app/static/") and relative.endswith(".css"))
                )
            return False
    return False


def _has_control_character(path: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in path)


def _is_safe_log_mountpoint_marker(path: str) -> bool:
    return path in SAFE_LOG_MOUNTPOINT_MARKERS


def _is_documentation_or_test(path: str) -> bool:
    return (
        path.startswith(("docs/", "tests/"))
        or path in {"AGENTS.md", "README.md", "CHANGELOG.md"}
        or path.endswith((".md", ".rst", ".txt"))
    )


def classify_changed_paths(paths: Iterable[str]) -> DeploymentDecision:
    """Return a fail-closed deployment decision for repository-relative paths."""
    normalised = tuple(sorted({_normalise_path(path) for path in paths if path}))
    if not normalised:
        return DeploymentDecision("skip", (), REASON_NO_CHANGES)

    if any(_has_control_character(path) for path in normalised):
        return DeploymentDecision("blocked", (), REASON_CONTROL_CHARACTER)
    blocked = tuple(
        path
        for path in normalised
        if _has_traversal_component(path)
        or path.startswith(BLOCKED_PREFIXES)
        or (
            path.startswith(tuple(SAFE_SERVICE_PREFIXES))
            and (
                not _is_allowed_service_path(path)
                or (
                    _is_sensitive_service_path(path)
                    and not _is_safe_log_mountpoint_marker(path)
                )
            )
        )
    )
    if blocked:
        return DeploymentDecision("blocked", (), REASON_BLOCKED_PATH)

    services = {
        service
        for path in normalised
        for prefix, service in SAFE_SERVICE_PREFIXES.items()
        if path.startswith(prefix)
    }
    shared_compose = set(normalised) & SHARED_COMPOSE_PATHS
    unknown = tuple(
        path
        for path in normalised
        if not _is_documentation_or_test(path)
        and not path.startswith(tuple(SAFE_SERVICE_PREFIXES))
        and path not in SHARED_COMPOSE_PATHS
        and path not in SAFE_NON_RUNTIME_PATHS
    )
    if unknown:
        return DeploymentDecision("blocked", (), REASON_UNKNOWN_RUNTIME)
    if set(normalised) & SHARED_COMPOSE_PATHS:
        return DeploymentDecision("blocked", (), REASON_BLOCKED_PATH)
    if services:
        return DeploymentDecision("deploy", tuple(sorted(services)), REASON_SAFE_SERVICE)
    return DeploymentDecision("skip", (), REASON_DOCUMENTATION)


def _changed_paths(base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-status", "-z", "--find-renames", f"{base}..{head}"],
        check=True,
        capture_output=True,
    )
    fields = result.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    paths: list[str] = []
    index = 0
    while index < len(fields):
        status = fields[index].decode("ascii")
        index += 1
        count = 2 if status[:1] in {"R", "C"} else 1
        if index + count > len(fields):
            raise ValueError(f"invalid git name-status record: {status}")
        paths.extend(field.decode("utf-8") for field in fields[index : index + count])
        index += count
    return paths


def _write_github_output(path: str, decision: DeploymentDecision) -> None:
    with open(path, "a", encoding="utf-8") as output:
        output.write(f"deploy_action={decision.action}\n")
        output.write(f"deploy_services={','.join(decision.services)}\n")
        output.write(f"deploy_reason={decision.reason}\n")


def main(argv: Sequence[str] | None = None, *, changed_paths: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--github-output", required=True)
    args = parser.parse_args(argv)
    paths = list(changed_paths) if changed_paths is not None else _changed_paths(args.base, args.head)
    decision = classify_changed_paths(paths)
    _write_github_output(args.github_output, decision)
    print(f"action={decision.action} services={','.join(decision.services)} reason={decision.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
