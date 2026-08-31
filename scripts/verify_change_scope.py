#!/usr/bin/env python3
"""Classify changed repository paths for the agent-loop verification policy."""

import argparse
import json
import sys
from pathlib import Path


SERVICE_PREFIXES = {
    "portal-web/": "portal",
    "system-agent/": "system-agent",
    "crawler-worker/": "crawler-worker",
    "homeops-executor/": "homeops-executor",
    "youtube-memo/": "youtube-memo",
    "book-memo/": "book-memo",
    "car-care-worker/": "car-care-worker",
}
DOCUMENTATION_PREFIXES = ("docs/",)
DOCUMENTATION_FILES = {"README.md", "AGENTS.md", "CLAUDE.md"}
SDD_REPORT_PREFIX = ".superpowers/sdd/"
AUTOMATION_PREFIXES = (".github/", "tests/")
INFRASTRUCTURE_PREFIXES = ("infra/k8s/",)
BLOCKED_PREFIXES = ("caddy/", "scripts/")
BLOCKED_INFRASTRUCTURE_FILES = {"docker-compose.yml", "docker-compose.n100.yml"}
APPROVED_RUNTIME_CONFIG_FILES = {"docker-compose.n100.yml", "caddy/Caddyfile"}
APPROVED_PORTAL_CUTOVER_RUNTIME_FILES = {
    "docker-compose.yml",
    "docker-compose.portal-bridge.yml",
    "scripts/deploy-n100.sh",
    "scripts/verify-n100-deployment-health.sh",
    "scripts/windows-bootstrap.sh",
}
DEPLOYMENT_WORKFLOW_FILES = {".github/workflows/deploy-n100.yml"}
BLOCKED_FILES = {
    "scripts/deploy-n100.sh",
    "crawler-worker/app/services/news_scheduler.py",
}
POLICY_MAINTENANCE_FILES = {
    "scripts/verify_change_scope.py",
    "scripts/run_change_harness.py",
    "scripts/summarize_token_measurements.py",
}


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        raise ValueError(message)


def _unique(paths: list[str]) -> list[str]:
    return list(dict.fromkeys(paths))


def _append_required_check(evidence: dict[str, object], check: str) -> None:
    required_checks = evidence["required_checks"]
    assert isinstance(required_checks, list)
    if check not in required_checks:
        required_checks.append(check)


def _has_traversal_component(path: str) -> bool:
    return any(component in {".", ".."} for component in path.split("/"))


def classify_paths(paths: list[str]) -> dict[str, object]:
    """Classify paths, retaining first-seen order in every result list."""
    changed_files = _unique(paths)
    evidence: dict[str, object] = {
        "changed_files": changed_files,
        "services": [],
        "documentation_files": [],
        "automation_files": [],
        "infrastructure_files": [],
        "blocked_files": [],
        "unclassified_files": [],
        "required_checks": [],
        "executed_checks": [],
        "missing_checks": [],
        "checks_validated": False,
    }

    for path in changed_files:
        if _has_traversal_component(path):
            evidence["unclassified_files"].append(path)
            continue

        if path in POLICY_MAINTENANCE_FILES:
            evidence["automation_files"].append(path)
            _append_required_check(evidence, "maintenance")
            continue

        if path in DEPLOYMENT_WORKFLOW_FILES:
            evidence["automation_files"].append(path)
            _append_required_check(evidence, "maintenance")
            for service in SERVICE_PREFIXES.values():
                _append_required_check(evidence, service)
            continue

        if path in APPROVED_RUNTIME_CONFIG_FILES or path in APPROVED_PORTAL_CUTOVER_RUNTIME_FILES:
            evidence["automation_files"].append(path)
            _append_required_check(evidence, "maintenance")
            for service in SERVICE_PREFIXES.values():
                _append_required_check(evidence, service)
            continue

        if (
            path in BLOCKED_FILES
            or path in BLOCKED_INFRASTRUCTURE_FILES
            or path.startswith(BLOCKED_PREFIXES)
        ):
            evidence["blocked_files"].append(path)
            continue

        service = next(
            (name for prefix, name in SERVICE_PREFIXES.items() if path.startswith(prefix)),
            None,
        )
        if service is not None:
            if service not in evidence["services"]:
                evidence["services"].append(service)
                _append_required_check(evidence, service)
            continue

        if (
            path in DOCUMENTATION_FILES
            or path.startswith(DOCUMENTATION_PREFIXES)
            or (path.startswith(SDD_REPORT_PREFIX) and path.endswith("-report.md"))
        ):
            evidence["documentation_files"].append(path)
            continue

        if path.startswith(AUTOMATION_PREFIXES):
            evidence["automation_files"].append(path)
            _append_required_check(evidence, "maintenance")
            continue

        if path.startswith(INFRASTRUCTURE_PREFIXES):
            evidence["infrastructure_files"].append(path)
            _append_required_check(evidence, "maintenance")
            continue

        evidence["unclassified_files"].append(path)

    return evidence


def _parse_git_name_status_z(contents: bytes) -> list[str]:
    fields = contents.split(b"\0")
    if fields[-1] != b"":
        raise ValueError("git-name-status-z input must end with a NUL byte")
    fields.pop()

    paths: list[str] = []
    index = 0
    while index < len(fields):
        status = fields[index].decode("ascii")
        index += 1
        if not status:
            raise ValueError("git-name-status-z input contains an empty status")

        path_count = 2 if status[0] in {"R", "C"} else 1
        if index + path_count > len(fields):
            raise ValueError(f"git-name-status-z input is missing a path for status {status}")
        for raw_path in fields[index : index + path_count]:
            if not raw_path:
                raise ValueError(f"git-name-status-z input contains an empty path for status {status}")
            paths.append(raw_path.decode("utf-8"))
        index += path_count

    return paths


def _read_paths(input_path: Path, input_format: str) -> list[str]:
    if input_format == "git-name-status-z":
        return _parse_git_name_status_z(input_path.read_bytes())
    return [path for path in input_path.read_text(encoding="utf-8").splitlines() if path]


def _parse_args() -> argparse.Namespace:
    parser = _ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument(
        "--input-format",
        choices=("paths", "git-name-status-z"),
        default="paths",
    )
    parser.add_argument("--test-result")
    parser.add_argument("--executed-checks", nargs="*")
    return parser.parse_args()


def main() -> int:
    try:
        args = _parse_args()
        evidence = classify_paths(_read_paths(args.input, args.input_format))
        if args.test_result is not None:
            evidence["test_result"] = args.test_result
        if args.executed_checks is not None:
            executed_checks = _unique(args.executed_checks)
            required_checks = evidence["required_checks"]
            assert isinstance(required_checks, list)
            evidence["executed_checks"] = executed_checks
            evidence["missing_checks"] = [
                check for check in required_checks if check not in executed_checks
            ]
            evidence["checks_validated"] = True
        print(json.dumps(evidence, ensure_ascii=False, separators=(",", ":")))
    except (OSError, UnicodeError, ValueError) as error:
        print(f"verify_change_scope: {error}", file=sys.stderr)
        return 1

    if (
        evidence["blocked_files"]
        or evidence["unclassified_files"]
        or evidence["missing_checks"]
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
