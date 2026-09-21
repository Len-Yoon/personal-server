#!/usr/bin/env python3
"""Create or verify isolated local virtual environments from the CI test matrix."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "tests" / "ci_test_matrix.json"
DEFAULT_VENV_ROOT = ROOT / ".venv"
METADATA_COMMAND = (
    "import importlib.metadata,json,sys; "
    "print(json.dumps({'python_version': f'{sys.version_info.major}.{sys.version_info.minor}', "
    "'packages': [{'name': distribution.metadata['Name'], 'version': distribution.version} "
    "for distribution in importlib.metadata.distributions()]}))"
)


def load_matrix() -> list[dict[str, object]]:
    """Load the CI-owned environment definition without duplicating it."""
    try:
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"CI test matrix cannot be read: {error}") from error
    if not isinstance(matrix, list):
        raise ValueError("CI test matrix must be a list")
    required = {"name", "python_version", "requirements", "extra_packages", "pythonpath", "test_command"}
    for entry in matrix:
        if not isinstance(entry, dict) or not required.issubset(entry):
            raise ValueError("CI test matrix entry is invalid")
    return matrix


def find_interpreter(version: object) -> str | None:
    """Return the CI-matching Python executable when it is installed locally."""
    return shutil.which(f"python{version}")


def environment_python(venv_root: Path, name: object) -> Path:
    return venv_root / str(name) / "bin" / "python"


def package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def declared_packages(entry: dict[str, object]) -> dict[str, str | None]:
    packages: dict[str, str | None] = {}
    candidates = list(entry["extra_packages"])
    requirements = str(entry["requirements"])
    if requirements:
        candidates.extend((ROOT / requirements).read_text(encoding="utf-8").splitlines())
    for line in candidates:
        candidate = line.strip()
        if not candidate or candidate.startswith(("#", "-")):
            continue
        match = re.match(r"(?P<name>[A-Za-z0-9_.-]+)(?:\[[^]]+\])?\s*(?:==\s*(?P<version>[^;\s]+))?", candidate)
        if match:
            packages[package_name(match.group("name"))] = match.group("version")
    return packages


def environment_metadata(python: Path) -> tuple[str, dict[str, str]]:
    result = subprocess.run(
        [str(python), "-c", METADATA_COMMAND],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    metadata = json.loads(result.stdout)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("python_version"), str):
        raise RuntimeError(f"{python}: environment metadata is invalid")
    packages = metadata.get("packages")
    if not isinstance(packages, list) or not all(
        isinstance(package, dict)
        and isinstance(package.get("name"), str)
        and isinstance(package.get("version"), str)
        for package in packages
    ):
        raise RuntimeError(f"{python}: installed package metadata is invalid")
    return metadata["python_version"], {
        package_name(package["name"]): package["version"] for package in packages
    }


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def create_or_update(entry: dict[str, object], venv_root: Path) -> None:
    name = str(entry["name"])
    venv_path = venv_root / name
    python = environment_python(venv_root, name)
    if not python.is_file():
        interpreter = find_interpreter(entry["python_version"])
        if interpreter is None:
            raise RuntimeError(f"{name}: python{entry['python_version']} interpreter is unavailable")
        run([interpreter, "-m", "venv", str(venv_path)])
    else:
        version, _ = environment_metadata(python)
        if version != entry["python_version"]:
            raise RuntimeError(f"{name}: expected Python {entry['python_version']}, found {version}")

    requirements = str(entry["requirements"])
    if requirements:
        run([str(python), "-m", "pip", "install", "--requirement", str(ROOT / requirements)])
    extras = [str(package) for package in entry["extra_packages"]]
    if extras:
        run([str(python), "-m", "pip", "install", *extras])
    run([str(python), "-m", "pip", "check"])


def check(entry: dict[str, object], venv_root: Path) -> bool:
    python = environment_python(venv_root, entry["name"])
    if not python.is_file() or not python.stat().st_mode & 0o111:
        print(f"[MISSING] {entry['name']}: {python}", file=sys.stderr)
        return False
    try:
        version, installed = environment_metadata(python)
        if version != entry["python_version"]:
            print(f"[INVALID] {entry['name']}: expected Python {entry['python_version']}, found {version}", file=sys.stderr)
            return False
        declared = declared_packages(entry)
        missing = set(declared) - set(installed)
        if missing:
            print(f"[INVALID] {entry['name']}: missing declared packages: {', '.join(sorted(missing))}", file=sys.stderr)
            return False
        wrong_versions = {
            package: version
            for package, version in declared.items()
            if version is not None and installed[package] != version
        }
        if wrong_versions:
            expected = ", ".join(f"{package}=={version}" for package, version in sorted(wrong_versions.items()))
            print(f"[INVALID] {entry['name']}: wrong declared package versions: {expected}", file=sys.stderr)
            return False
        run([str(python), "-m", "pip", "check"])
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError):
        return False
    return True


def parse_args(arguments: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--go", action="store_true", help="create/update environments and install declared dependencies")
    operation.add_argument("--check", action="store_true", help="verify prepared environments without installing packages")
    parser.add_argument("--venv-root", type=Path, default=DEFAULT_VENV_ROOT)
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(arguments)
    try:
        matrix = load_matrix()
        if args.go:
            for entry in matrix:
                create_or_update(entry, args.venv_root)
            return 0
        return 0 if all(check(entry, args.venv_root) for entry in matrix) else 1
    except (RuntimeError, subprocess.CalledProcessError, ValueError) as error:
        print(f"[FAIL] {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
