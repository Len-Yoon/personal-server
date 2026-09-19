#!/usr/bin/env python3
"""Run the CI Python test groups locally with CI-equivalent isolation."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "tests" / "ci_test_matrix.json"
DEFAULT_VENV_ROOT = ROOT / ".venv"
REQUIRED_FIELDS = {
    "name",
    "python_version",
    "requirements",
    "extra_packages",
    "pythonpath",
    "test_command",
}


def load_matrix() -> list[dict[str, object]]:
    """Load and validate the single CI/local Python test matrix."""
    try:
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load CI test matrix: {error}") from error

    if not isinstance(matrix, list) or len(matrix) != 9:
        raise ValueError("CI test matrix must define exactly nine groups")

    names: set[str] = set()
    for entry in matrix:
        if not isinstance(entry, dict) or set(entry) != REQUIRED_FIELDS:
            raise ValueError("CI test matrix entry schema is invalid")
        if not isinstance(entry["name"], str) or not entry["name"] or entry["name"] in names:
            raise ValueError("CI test matrix group names must be unique non-empty strings")
        if entry["python_version"] not in {"3.11", "3.12"}:
            raise ValueError("CI test matrix Python version is invalid")
        if not all(isinstance(entry[field], str) for field in ("requirements", "pythonpath", "test_command")):
            raise ValueError("CI test matrix string field is invalid")
        if not isinstance(entry["extra_packages"], list) or not all(isinstance(item, str) for item in entry["extra_packages"]):
            raise ValueError("CI test matrix extra packages field is invalid")
        command = shlex.split(entry["test_command"])
        if len(command) < 2 or command[0] != "python3":
            raise ValueError("CI test matrix command must begin with python3")
        names.add(entry["name"])
    return matrix


def parse_args(matrix: list[dict[str, object]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=[entry["name"] for entry in matrix], action="append")
    parser.add_argument("--list", action="store_true", help="list available CI-equivalent Python suites and exit")
    parser.add_argument("--dry-run", action="store_true", help="print subprocess commands without executing tests")
    parser.add_argument("--github-matrix", action="store_true", help="print the GitHub Actions matrix JSON and exit")
    parser.add_argument(
        "--venv-root",
        type=Path,
        default=DEFAULT_VENV_ROOT,
        help="use per-service local virtual environments below this directory when available",
    )
    return parser.parse_args()


def command_for(entry: dict[str, object], venv_root: Path = DEFAULT_VENV_ROOT) -> tuple[list[str], dict[str, str]]:
    command = shlex.split(str(entry["test_command"]))
    venv_python = venv_root / str(entry["name"]) / "bin" / "python"
    command[0] = str(venv_python) if venv_python.is_file() and os.access(venv_python, os.X_OK) else f"python{entry['python_version']}"
    environment = {"PATH": os.environ.get("PATH", os.defpath), "PYTHONPATH": str(ROOT / str(entry["pythonpath"]))}
    return command, environment


def run_suite(entry: dict[str, object], dry_run: bool, venv_root: Path = DEFAULT_VENV_ROOT) -> int:
    command, environment = command_for(entry, venv_root)
    name = str(entry["name"])
    rendered = " ".join(shlex.quote(part) for part in command)
    if dry_run:
        print(f"[DRY RUN] {name} PYTHONPATH={environment['PYTHONPATH']} {rendered}")
        return 0

    try:
        result = subprocess.run(command, cwd=ROOT, env=environment)
    except FileNotFoundError:
        print(f"[FAIL] {name} (missing interpreter: {command[0]})")
        return 1
    if result.returncode:
        print(f"[FAIL] {name} (exit {result.returncode})")
        return 1
    print(f"[PASS] {name}")
    return 0


def main() -> int:
    try:
        matrix = load_matrix()
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    args = parse_args(matrix)
    if args.github_matrix:
        print(json.dumps({"include": matrix}, separators=(",", ":")))
        return 0
    if args.list:
        print("\n".join(str(entry["name"]) for entry in matrix))
        return 0

    selected_names = set(args.suite or ())
    selected = [entry for entry in matrix if not selected_names or entry["name"] in selected_names]
    failures = sum(run_suite(entry, args.dry_run, args.venv_root) for entry in selected)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
