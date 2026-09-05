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
BLOCKED_PREFIXES = (
    "portal-web/",
    "caddy/",
    "system-agent/",
    "homeops-executor/",
    "infra/k8s/",
)
SHARED_COMPOSE_PATHS = {"docker-compose.yml", "docker-compose.n100.yml"}

DeploymentAction = Literal["skip", "deploy", "blocked"]


@dataclass(frozen=True)
class DeploymentDecision:
    action: DeploymentAction
    services: tuple[str, ...]
    reason: str


def _normalise_path(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")


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
        return DeploymentDecision("skip", (), "변경 경로 없음")

    blocked = tuple(path for path in normalised if path.startswith(BLOCKED_PREFIXES))
    if blocked:
        return DeploymentDecision("blocked", (), f"비허용 경로 변경: {', '.join(blocked)}")

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
    )
    if unknown:
        return DeploymentDecision("blocked", (), f"검토가 필요한 운영 경로 변경: {', '.join(unknown)}")
    if shared_compose:
        services.update(SAFE_SERVICES)
    if services:
        return DeploymentDecision("deploy", tuple(sorted(services)), "허용된 Compose 서비스 변경")
    return DeploymentDecision("skip", (), "문서·테스트·비운영 경로만 변경")


def _changed_paths(base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base}..{head}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


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
