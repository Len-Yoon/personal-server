#!/usr/bin/env python3
"""Run the change-scope policy check and emit review evidence."""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

try:
    from scripts.verify_change_scope import SERVICE_PREFIXES
except ModuleNotFoundError:  # Direct execution sets sys.path to scripts/.
    from verify_change_scope import SERVICE_PREFIXES


REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_CHECKER = REPO_ROOT / "scripts" / "verify_change_scope.py"
KNOWN_CHECKS = frozenset(SERVICE_PREFIXES.values()) | {"maintenance"}
POLICY_LIST_KEYS = (
    "services",
    "required_checks",
    "executed_checks",
    "blocked_files",
    "unclassified_files",
    "missing_checks",
)


def _parse_check_results(values: Iterable[str]) -> dict[str, str]:
    results: dict[str, str] = {}
    for value in values:
        name, separator, result = value.partition("=")
        if not separator or not name or not result:
            raise ValueError(f"check result must be NAME=RESULT: {value}")
        if name not in KNOWN_CHECKS:
            raise ValueError(f"unknown check name: {name}")
        if result not in {"success", "failure"}:
            raise ValueError(f"check result must be success or failure: {value}")
        results[name] = result
    return results


def _input_error_evidence(error: Exception) -> dict:
    return {
        "schema_version": "1",
        "policy_evidence": None,
        "check_results": [],
        "work_status": "input_error",
        "human_review_required": True,
        "human_review_reasons": [str(error)],
        "summary": {
            "services": [],
            "required_checks": [],
            "missing_checks": [],
            "failed_checks": [],
            "blocked_files": [],
            "unclassified_files": [],
        },
    }


def _validate_policy_evidence(evidence: object, executed_checks: list[str]) -> dict:
    if not isinstance(evidence, dict):
        raise ValueError("policy evidence must be a JSON object")
    for key in POLICY_LIST_KEYS:
        value = evidence.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"policy evidence has invalid {key}")
    if not isinstance(evidence.get("checks_validated"), bool):
        raise ValueError("policy evidence has invalid checks_validated")
    if evidence["executed_checks"] != executed_checks:
        raise ValueError("policy evidence executed_checks does not match check results")
    return evidence


def _run_policy(
    paths: list[str], input_format: str, results: dict[str, str], *, input_contents: bytes | None = None
) -> tuple[int, dict]:
    mode = "wb" if input_contents is not None else "w"
    with tempfile.NamedTemporaryFile(mode, encoding=None if mode == "wb" else "utf-8", delete=False) as handle:
        handle.write(input_contents if input_contents is not None else "\n".join(paths))
        input_path = Path(handle.name)

    try:
        command = [
            sys.executable,
            str(POLICY_CHECKER),
            "--input",
            str(input_path),
            "--input-format",
            input_format,
        ]
        command.extend(["--executed-checks", *results])
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        evidence = json.loads(completed.stdout) if completed.stdout else {}
        return completed.returncode, evidence
    finally:
        input_path.unlink(missing_ok=True)


def build_evidence(
    paths: list[str], *, input_format: str = "paths", check_results: Iterable[str] = (),
    input_contents: bytes | None = None,
) -> tuple[int, dict]:
    try:
        results = _parse_check_results(check_results)
        policy_code, raw_policy_evidence = _run_policy(
            paths, input_format, results, input_contents=input_contents
        )
        if policy_code not in {0, 2}:
            raise ValueError(f"policy checker exited with code {policy_code}")
        executed = list(results)
        policy_evidence = _validate_policy_evidence(raw_policy_evidence, executed)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        return 1, _input_error_evidence(error)

    required = policy_evidence["required_checks"]
    missing = [name for name in required if name not in results]
    failed = [name for name in required if results.get(name) == "failure"]

    reasons: list[str] = []
    if policy_evidence.get("blocked_files"):
        reasons.append("blocked_files")
    if policy_evidence.get("unclassified_files"):
        reasons.append("unclassified_files")
    if missing:
        reasons.append("missing_checks")
    if failed:
        reasons.append("failed_checks")

    if policy_evidence["blocked_files"] or policy_evidence["unclassified_files"]:
        work_status = "blocked"
    elif missing:
        work_status = "verification_incomplete"
    elif failed:
        work_status = "verification_failed"
    else:
        work_status = "ready_for_review"

    ready = work_status == "ready_for_review"
    evidence = {
        "schema_version": "1",
        "policy_evidence": policy_evidence,
        "check_results": [
            {"name": name, "result": result} for name, result in results.items()
        ],
        "work_status": work_status,
        "human_review_required": not ready,
        "human_review_reasons": reasons,
        "summary": {
            "services": policy_evidence["services"],
            "required_checks": required,
            "missing_checks": missing,
            "failed_checks": failed,
            "blocked_files": policy_evidence["blocked_files"],
            "unclassified_files": policy_evidence["unclassified_files"],
        },
    }
    return (0 if ready else 2), evidence


def run_harness(
    paths: list[str], *, check_results: Iterable[str] = (), input_format: str = "paths"
) -> tuple[int, dict]:
    """Convenience API used by tests and callers in the repository."""
    return build_evidence(paths, input_format=input_format, check_results=check_results)


def format_agent_context(evidence: dict) -> str:
    """Return a compact, prompt-safe summary of the harness evidence."""
    summary = evidence["summary"]
    lines = [f"Work status: {evidence['work_status']}"]
    for label, key in (
        ("Required checks", "required_checks"),
        ("Missing checks", "missing_checks"),
        ("Failed checks", "failed_checks"),
        ("Blocked paths", "blocked_files"),
        ("Unclassified paths", "unclassified_files"),
    ):
        if summary[key]:
            lines.append(f"{label}: {', '.join(summary[key])}")

    if evidence["work_status"] == "verification_incomplete":
        action = "Run the missing required checks and rerun with --check-result values."
    elif evidence["work_status"] == "verification_failed":
        action = "Fix the failed checks and rerun with --check-result values."
    elif evidence["work_status"] == "blocked":
        action = "Resolve the blocked or unclassified paths before continuing."
    elif evidence["work_status"] == "ready_for_review":
        action = "Request human review."
    else:
        action = "Correct the command input and rerun the harness."
    lines.append(f"Next action: {action}")
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    class _ArgumentParser(argparse.ArgumentParser):
        def error(self, message: str) -> None:
            raise ValueError(message)

    parser = _ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument(
        "--input-format", choices=("paths", "git-name-status-z"), default="paths"
    )
    parser.add_argument("--check-result", action="append", default=[])
    parser.add_argument("--agent-context", action="store_true")
    return parser.parse_args()


def main() -> int:
    try:
        args = _parse_args()
        paths = (
            args.input.read_text(encoding="utf-8").splitlines()
            if args.input_format == "paths"
            else []
        )
        code, evidence = build_evidence(
            paths,
            input_format=args.input_format,
            check_results=args.check_result,
            input_contents=args.input.read_bytes()
            if args.input_format == "git-name-status-z"
            else None,
        )
        if args.agent_context:
            print(format_agent_context(evidence))
        else:
            print(json.dumps(evidence, ensure_ascii=False, separators=(",", ":")))
        return code
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps(_input_error_evidence(error), ensure_ascii=False, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
