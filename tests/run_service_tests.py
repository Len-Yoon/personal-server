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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.verify_change_scope import SERVICE_PREFIXES, _read_changes, classify_paths


MATRIX_PATH = ROOT / "tests" / "ci_test_matrix.json"
DOCUMENTATION_COMMAND = "python3 -m unittest " + " ".join((
    "tests.test_documentation_index",
    "tests.test_documentation_links",
    "tests.test_compose_config",
    "tests.test_verify_change_scope",
    "tests.test_deploy_n100",
    "tests.test_change_harness",
    "tests.test_token_measurements",
    "tests.test_n100_remote_dev",
    "tests.test_n100_safe_deployment",
    "tests.test_run_service_tests",
))
DEFAULT_VENV_ROOT = ROOT / ".venv"
REQUIRED_FIELDS = {
    "name",
    "python_version",
    "requirements",
    "extra_packages",
    "pythonpath",
    "test_command",
}
_UNITTEST_PREFIX = ("python3", "-m", "unittest")
_SCOPE_LIST_FIELDS = (
    "changed_files", "services", "documentation_files", "automation_files",
    "infrastructure_files", "blocked_files", "unclassified_files", "required_checks",
)


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


def discover_test_files(root: Path = ROOT) -> set[str]:
    tests_root = (root / "tests").resolve()
    files: set[str] = set()
    for path in tests_root.rglob("test_*.py"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(tests_root)
        except ValueError as error:
            raise ValueError(f"test file escapes tests root: {path}") from error
        files.add((Path("tests") / relative).as_posix())
    return files


def _inside_root(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"test path escapes repository root: {path}") from error
    return resolved


def files_for_test_command(command: str, root: Path = ROOT) -> set[str]:
    tokens = shlex.split(command)
    if tokens[:3] != list(_UNITTEST_PREFIX):
        raise ValueError("test command must begin with python3 -m unittest")
    if len(tokens) < 4:
        raise ValueError("test command has no unittest target")

    tests_root = (root / "tests").resolve()
    targets = tokens[3:]
    if targets[0] == "discover":
        if len(targets) != 3 or targets[1] != "-s":
            raise ValueError("discover command syntax is unsupported")
        target = targets[2]
        target_path = Path(target)
        if target_path.is_absolute() or ".." in target_path.parts:
            raise ValueError("discover target must stay inside tests")
        if target_path.as_posix() != "tests/car_care_worker":
            raise ValueError("discover target is not allowed")
        directory = _inside_root(root / target_path, root)
        try:
            directory.relative_to(tests_root)
        except ValueError as error:
            raise ValueError("discover target must stay inside tests") from error
        if not directory.is_dir():
            raise ValueError(f"discover target does not exist: {target}")
        nested_directories = [
            path
            for path in directory.iterdir()
            if path.is_dir() and any(path.rglob("test_*.py"))
        ]
        if nested_directories:
            names = ", ".join(path.name for path in sorted(nested_directories))
            raise ValueError(f"discover target contains nested directories: {names}")
        files = sorted(directory.glob("test_*.py"))
        if not files:
            raise ValueError("discover target contains no test files")
        return {(path.resolve().relative_to(root.resolve())).as_posix() for path in files}

    files: set[str] = set()
    for module in targets:
        if module.startswith("-") or not module.startswith("tests."):
            raise ValueError(f"unsupported unittest target: {module}")
        parts = module.split(".")
        if any(not part or not part.isidentifier() for part in parts):
            raise ValueError(f"invalid unittest module: {module}")
        path = _inside_root(root / Path(*parts).with_suffix(".py"), root)
        try:
            relative = path.relative_to(root.resolve())
        except ValueError as error:
            raise ValueError(f"unittest module escapes repository root: {module}") from error
        try:
            path.relative_to(tests_root)
        except ValueError as error:
            raise ValueError(f"unittest module must stay inside tests: {module}") from error
        if not path.is_file():
            raise ValueError(f"unittest module does not exist: {module}")
        normalized = relative.as_posix()
        if normalized in files:
            raise ValueError(f"duplicate unittest target: {normalized}")
        files.add(normalized)
    return files


def validate_matrix_coverage(matrix: list[dict[str, object]]) -> set[str]:
    expected = discover_test_files()
    owners: dict[str, str] = {}
    for entry in matrix:
        group = str(entry["name"])
        for path in files_for_test_command(str(entry["test_command"])):
            previous = owners.get(path)
            if previous is not None:
                raise ValueError(f"duplicate matrix coverage for {path}: {previous}, {group}")
            owners[path] = group
    assigned = set(owners)
    missing = sorted(expected - assigned)
    extra = sorted(assigned - expected)
    if missing or extra:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("extra: " + ", ".join(extra))
        raise ValueError("CI test matrix coverage mismatch (" + "; ".join(details) + ")")
    return assigned


def select_ci_suites(
    evidence: dict[str, object], matrix: list[dict[str, object]],
    *, deleted_paths: set[str] | None = None,
) -> tuple[str, list[str]]:
    """Select bounded suites from validated scope evidence; uncertainty runs all suites."""
    if not isinstance(evidence, dict) or any(
        not isinstance(evidence.get(field), list)
        or any(not isinstance(value, str) for value in evidence[field])
        for field in _SCOPE_LIST_FIELDS
    ):
        raise ValueError("scope evidence is incomplete")

    changed = set(evidence["changed_files"])
    categories = (
        "documentation_files", "automation_files", "infrastructure_files",
        "blocked_files", "unclassified_files",
    )
    classified = set().union(*(evidence[field] for field in categories))
    service_paths = {
        path for path in changed if any(path.startswith(prefix) for prefix in SERVICE_PREFIXES)
    }
    if changed != classified | service_paths:
        raise ValueError("scope evidence does not classify every changed path")

    independently_classified = classify_paths(evidence["changed_files"], deleted_paths=deleted_paths)
    if any(
        evidence[field] != independently_classified[field]
        for field in (*_SCOPE_LIST_FIELDS,)
    ):
        raise ValueError("scope evidence classification does not match changed paths")

    all_names = [str(entry["name"]) for entry in matrix]
    known = set(all_names)
    if "maintenance" not in known or not set(evidence["required_checks"]) <= known:
        raise ValueError("scope evidence requires an unknown CI suite")
    full = ("full", all_names)
    if (
        not changed or evidence["blocked_files"] or evidence["unclassified_files"]
        or evidence["infrastructure_files"]
        or any(Path(path).name in {"Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", "package.json", "package-lock.json"}
               or Path(path).name.startswith("requirements") for path in service_paths)
    ):
        return full

    owners = {
        path: str(entry["name"])
        for entry in matrix
        for path in files_for_test_command(str(entry["test_command"]))
    }
    service_names = set(SERVICE_PREFIXES.values())
    touched = set(evidence["services"])
    if not touched <= service_names:
        raise ValueError("scope evidence names an unknown service")
    for path in evidence["automation_files"]:
        owner = owners.get(path) if path.startswith("tests/") else None
        if owner not in service_names:
            return full
        touched.add(owner)

    if len(touched) > 1:
        return full
    if touched:
        selected = [name for name in all_names if name in touched or name == "maintenance"]
        if set(evidence["required_checks"]) <= set(selected):
            return "service", selected
        return full
    if evidence["documentation_files"]:
        if not evidence["required_checks"]:
            return "documentation", ["documentation"]
        return full
    return full


def parse_args(matrix: list[dict[str, object]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=[entry["name"] for entry in matrix], action="append")
    parser.add_argument("--list", action="store_true", help="list available CI-equivalent Python suites and exit")
    parser.add_argument("--dry-run", action="store_true", help="print subprocess commands without executing tests")
    parser.add_argument("--github-matrix", action="store_true", help="print the GitHub Actions matrix JSON and exit")
    parser.add_argument("--scope-evidence", type=Path, help="select GitHub matrix suites from scope JSON")
    parser.add_argument("--changes-input", type=Path, help="name-status-z changes used to build scope evidence")
    parser.add_argument(
        "--venv-root",
        type=Path,
        default=DEFAULT_VENV_ROOT,
        help="use per-service local virtual environments below this directory when available",
    )
    return parser.parse_args()


def command_for(entry: dict[str, object], venv_root: Path = DEFAULT_VENV_ROOT) -> tuple[list[str], dict[str, str]]:
    files_for_test_command(str(entry["test_command"]))
    command = shlex.split(str(entry["test_command"]))
    venv_python = venv_root / str(entry["name"]) / "bin" / "python"
    command[0] = str(venv_python) if venv_python.is_file() and os.access(venv_python, os.X_OK) else f"python{entry['python_version']}"
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONPATH": str(ROOT / str(entry["pythonpath"])),
        "USER": "audit_fixture",
    }
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
        validate_matrix_coverage(matrix)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    args = parse_args(matrix)
    if args.github_matrix:
        if args.scope_evidence is None:
            print(json.dumps({"include": matrix}, separators=(",", ":")))
            return 0
        try:
            evidence = json.loads(args.scope_evidence.read_text(encoding="utf-8"))
            deleted_paths = None
            if args.changes_input is not None:
                changes = _read_changes(args.changes_input, "git-name-status-z")
                if list(dict.fromkeys(path for _, path in changes)) != evidence.get("changed_files"):
                    raise ValueError("scope evidence changed paths do not match Git input")
                deleted_paths = {path for status, path in changes if status == "D"}
            mode, selected = select_ci_suites(evidence, matrix, deleted_paths=deleted_paths)
        except (OSError, ValueError) as error:
            print(f"CI selection failed: {error}", file=sys.stderr)
            return 2
        include = [entry for entry in matrix if entry["name"] in selected]
        if mode == "documentation":
            files_for_test_command(DOCUMENTATION_COMMAND)
            include = [{
                "name": "documentation",
                "python_version": "3.11",
                "requirements": "",
                "extra_packages": [],
                "pythonpath": ".",
                "test_command": DOCUMENTATION_COMMAND,
            }]
        print(json.dumps({
            "include": include,
            "selected_checks": selected,
            "mode": mode,
        }, separators=(",", ":")))
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
