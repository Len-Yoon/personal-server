#!/usr/bin/env python3
"""Measure selected production branches and kill bounded mutants in disposable fixtures."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "tests/verification_quality.json"
# Deliberately individual code files: never recursively copy data, credentials or a checkout.
COPY_ALLOWLIST = (
    "scripts/run_verification_quality.py", "tests/__init__.py", "tests/_test_support.py",
    "tests/test_portal_security.py", "tests/test_file_access.py", "tests/test_file_management.py",
    "tests/test_validate_backup_evidence.py", "tests/test_portal_improvements.py",
    "tests/test_security_full_paths.py", "tests/test_file_store_full_paths.py",
    "tests/test_backup_full_paths.py",
    "portal-web/app/services/__init__.py",
    "portal-web/app/services/security.py", "portal-web/app/services/file_store.py",
    "portal-web/app/services/admin_status.py", "portal-web/app/services/file_management.py",
    "infra/k8s/tools/validate-backup-evidence.py",
)
TARGETS = (
    "portal-web/app/services/security.py", "portal-web/app/services/file_store.py",
    "infra/k8s/tools/validate-backup-evidence.py",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_fixture(source: Path, destination: Path) -> dict[str, str]:
    if source.is_symlink():
        raise ValueError("symlink source boundary")
    source = source.resolve()
    hashes = {}
    for relative in COPY_ALLOWLIST:
        original = source / relative
        # resolve() alone would hide a symlink traversing back inside the checkout.
        parts = (original, *original.parents)
        if any(part.is_symlink() for part in parts if part.is_relative_to(source)):
            raise ValueError(f"symlink copy boundary: {relative}")
        if not original.resolve().is_relative_to(source.resolve()) or not original.is_file():
            raise ValueError(f"missing/escaped copy input: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(original.read_bytes())
        hashes[relative] = digest(original)
    return hashes


def fixture_environment(root: Path) -> dict[str, str]:
    # No inherited application variables, proxy, credentials, Python startup hooks or HOME.
    home = root / "fixture-home"
    temporary = root / "fixture-tmp"
    home.mkdir(exist_ok=True)
    temporary.mkdir(exist_ok=True)
    return {
        "PATH": os.defpath, "HOME": str(home), "TMPDIR": str(temporary),
        "TEMP": str(temporary), "TMP": str(temporary), "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1", "APP_ENV": "development",
        "SECURITY_LOG_PATH": str(root / "fixture-data/security.log"),
        "AUTH_RATE_LIMIT_STATE_PATH": str(root / "fixture-data/auth.json"),
        "FILE_STORAGE_PATH": str(root / "fixture-data/files"),
    }


def apply_mutant(root: Path, mutation: dict) -> None:
    if mutation["target"] not in TARGETS:
        raise ValueError("mutation target is outside production allowlist")
    target = root / mutation["target"]
    text = target.read_text(encoding="utf-8")
    if mutation["before"] == mutation["after"] or text.count(mutation["before"]) != 1:
        raise ValueError("mutation must change exactly one matching source fragment")
    target.write_text(text.replace(mutation["before"], mutation["after"], 1), encoding="utf-8")


def acceptable_result(result: dict, expected_ids: list[str], *, mutant: bool) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("test_ids") != expected_ids or not expected_ids:
        return False
    if result.get("run") != len(expected_ids) or result.get("errors") or result.get("skipped"):
        return False
    if result.get("unexpected_successes") or result.get("expected_failures"):
        return False
    if any(type(result.get(key)) is not int or result[key] < 0 for key in
           ("run", "errors", "skipped", "failures", "unexpected_successes", "expected_failures")):
        return False
    return result["failures"] > 0 if mutant else result["failures"] == 0


def validate_config(config: dict) -> None:
    if set(config["targets"]) != set(TARGETS):
        raise ValueError("coverage must measure every fixed production target")
    def floors_valid(floors):
        for key in ("statement_min", "branch_min"):
            value = floors[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 100:
                raise ValueError("coverage floors must be finite numbers in (0,100]")
        for nested in floors.get("functions", {}).values():
            floors_valid(nested)
    for floors in config["targets"].values():
        floors_valid(floors)
        if not floors.get("functions"):
            raise ValueError("each production target requires a critical function floor")
        lines = floors.get("expected_missing_lines")
        arcs = floors.get("expected_missing_branches")
        if (not isinstance(lines, list) or any(type(line) is not int or line <= 0 for line in lines)
                or len(lines) != len(set(lines)) or not isinstance(arcs, list)
                or any(not isinstance(arc, list) or len(arc) != 2
                       or any(type(value) is not int or value == 0 for value in arc) for arc in arcs)
                or len(arcs) != len({tuple(arc) for arc in arcs})):
            raise ValueError("every target requires exact missing line/branch lists")
        proof = floors.get("unreachable_proof")
        if lines or arcs:
            if (not isinstance(proof, dict) or not isinstance(proof.get("reason"), str)
                    or not proof["reason"].strip() or not isinstance(proof.get("source_sha256"), str)
                    or len(proof["source_sha256"]) != 64
                    or any(char not in "0123456789abcdef" for char in proof["source_sha256"])):
                raise ValueError("remaining gaps require a source-bound unreachable proof")
    if type(config["timeout_seconds"]) is not int or not 1 <= config["timeout_seconds"] <= 300:
        raise ValueError("timeout must be an integer in [1,300]")
    if not config["test_ids"] or len(config["test_ids"]) != len(set(config["test_ids"])):
        raise ValueError("baseline test IDs must be nonempty and unique")
    if not 3 <= len(config["mutants"]) <= 4 or {item["target"] for item in config["mutants"]} != set(TARGETS):
        raise ValueError("bounded mutation suite must cover all three production targets")
    if len({item["id"] for item in config["mutants"]}) != len(config["mutants"]):
        raise ValueError("mutation IDs must be unique")


def execution_status(result: dict, test_ids: list[str], returncode: int, *, mutant: bool) -> str:
    if mutant and returncode == 1 and acceptable_result(result, test_ids, mutant=True):
        return "killed"
    if returncode == 0 and acceptable_result(result, test_ids, mutant=False):
        return "survived" if mutant else "pass"
    return "invalid" if mutant else "baseline_failed"


def coverage_verdict(report: dict, targets: dict) -> tuple[dict, list[str]]:
    failures = []
    measured = {}
    if report.get("meta", {}).get("branch_coverage") is not True:
        failures.append("branch measurement missing")
    for name, thresholds in targets.items():
        data = report.get("files", {}).get(name)
        if not data:
            failures.append(f"missing production target: {name}")
            continue
        summary = data["summary"]
        statements, branches = summary["num_statements"], summary.get("num_branches", 0)
        stmt = 100 * summary["covered_lines"] / statements if statements else None
        branch = 100 * summary.get("covered_branches", 0) / branches if branches else None
        measured[name] = {**data, "statement_percent": stmt, "branch_percent": branch,
                          "thresholds": thresholds}
        if "expected_missing_lines" in thresholds:
            if data.get("excluded_lines"):
                failures.append(f"excluded production lines: {name}")
            baseline = thresholds.get("baseline_counts", {})
            if any(summary.get(key) != baseline.get(key) for key in ("num_statements", "num_branches")):
                failures.append(f"production coverage denominator drift: {name}")
        # Keep structurally unreachable code in the measured denominator. Match
        # every remaining line/arc exactly; never hide new gaps behind a floor.
        for key in ("missing_lines", "missing_branches"):
            expected = thresholds.get(f"expected_{key}")
            if expected is not None and sorted(data.get(key, [])) != sorted(expected):
                failures.append(f"unexpected {key}: {name}")
        if stmt is None or stmt < thresholds["statement_min"]:
            failures.append(f"statement floor: {name}")
        if branch is None or branch < thresholds["branch_min"]:
            failures.append(f"branch floor/missing branches: {name}")
        for function, floors in thresholds.get("functions", {}).items():
            nested, errors = coverage_verdict(
                {"meta": {"branch_coverage": True}, "files": data.get("functions", {})},
                {function: floors},
            )
            measured[name].setdefault("critical_functions", {}).update(nested)
            failures.extend(f"{name}:{error}" for error in errors)
    return measured, failures


def worker(root: Path, request: dict) -> int:
    """Only selected existing unittest IDs; validator subprocesses receive their own tracer."""
    import coverage
    sys.path.insert(0, str(root))
    ids = request["test_ids"]
    suite = unittest.defaultTestLoader.loadTestsFromNames(ids)

    def flatten(tests):
        for test in tests:
            if isinstance(test, unittest.TestSuite):
                yield from flatten(test)
            else:
                yield test.id()

    collected = list(flatten(suite))
    cov = None
    if request["coverage"]:
        cov = coverage.Coverage(config_file=str(root / "quality.coveragerc"))
        cov.start()
    original_run = subprocess.run

    def traced_run(args, **kwargs):
        if request["coverage"] and len(args) > 1 and args[1] == str(root / TARGETS[2]):
            args = [args[0], "-m", "coverage", "run", "--rcfile", str(root / "quality.coveragerc"),
                    "--parallel-mode", *args[1:]]
        return original_run(args, **kwargs)

    # No test in the fixture needs network; fail before any connection is attempted.
    def denied(*args, **kwargs):
        raise RuntimeError("network access prohibited in quality fixture")

    with patch("subprocess.run", side_effect=traced_run), patch("socket.socket.connect", side_effect=denied), \
            patch("socket.create_connection", side_effect=denied):
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    if cov:
        cov.stop()
        cov.save()
    record = {
        "test_ids": collected, "run": result.testsRun, "failures": len(result.failures),
        "errors": len(result.errors), "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures), "unexpected_successes": len(result.unexpectedSuccesses),
        "failure_test_ids": [test.id() for test, _ in result.failures],
    }
    (root / "worker-result.json").write_text(json.dumps(record), encoding="utf-8")
    if cov:
        combined = coverage.Coverage(config_file=str(root / "quality.coveragerc"))
        combined.combine()
        combined.load()
        combined.json_report(outfile=str(root / "coverage.json"), pretty_print=True)
    return 0 if result.wasSuccessful() else 1


def execute_fixture(source: Path, config: dict, test_ids: list[str], mutation: dict | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="verification-quality-") as directory:
        root = Path(directory).resolve()
        hashes = copy_fixture(source, root)
        env = fixture_environment(root)
        if mutation:
            apply_mutant(root, mutation)
        (root / "quality.coveragerc").write_text(
            "[run]\nbranch = True\nparallel = True\nrelative_files = True\n"
            "include =\n    " + "\n    ".join(TARGETS) + "\n", encoding="utf-8")
        request = {"test_ids": test_ids, "coverage": mutation is None}
        (root / "worker-request.json").write_text(json.dumps(request), encoding="utf-8")
        command = [sys.executable, "-I", str(root / "scripts/run_verification_quality.py"), "--_worker", str(root)]
        process = subprocess.Popen(command, cwd=root, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            output, _ = process.communicate(timeout=config["timeout_seconds"])
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            return {"status": "timeout", "original_hashes": hashes}
        result_path = root / "worker-result.json"
        result = json.loads(result_path.read_text()) if result_path.exists() else {}
        status = execution_status(result, test_ids, process.returncode, mutant=mutation is not None)
        record = {"status": status,
                  "original_hashes": hashes, "result": result, "returncode": process.returncode,
                  "output": output.replace(str(root), "<fixture>"), "test_ids": test_ids}
        if mutation:
            record["mutated_hash"] = digest(root / mutation["target"])
        elif (root / "coverage.json").is_file():
            record["coverage"] = json.loads((root / "coverage.json").read_text())
        return record


def run_quality(source: Path, config: dict, output: Path) -> int:
    output.mkdir(parents=True, exist_ok=True)
    before = {}
    completed = False
    report = {"schema_version": 1, "status": "failed",
              "coverage_version": config.get("coverage_version") if isinstance(config, dict) else None,
              "source_hashes": before, "baseline": {}, "mutants": [], "failures": []}
    # Replace any previous successful artifact before running new work.
    (output / "quality.json").write_text(json.dumps({**report, "failures": ["run incomplete"]}), encoding="utf-8")
    try:
        if not isinstance(config, dict):
            raise ValueError("quality configuration must be a JSON object")
        before.update({name: digest(source / name) for name in COPY_ALLOWLIST})
        import coverage
        if coverage.__version__ != config["coverage_version"]:
            raise ValueError("install the pinned tests/requirements.quality.txt coverage version")
        validate_config(config)
        for name, thresholds in config["targets"].items():
            proof = thresholds.get("unreachable_proof")
            if proof and digest(source / name) != proof["source_sha256"]:
                raise ValueError(f"unreachable source proof is stale: {name}")
        baseline = execute_fixture(source, config, config["test_ids"])
        report["baseline"] = baseline
        if baseline["status"] != "pass":
            raise ValueError("baseline must pass with exact nonempty collection and zero skips/errors")
        raw_coverage = baseline.pop("coverage", {})
        (output / "coverage.json").write_text(json.dumps(raw_coverage, indent=2), encoding="utf-8")
        report["coverage"], failures = coverage_verdict(raw_coverage, config["targets"])
        report["failures"].extend(failures)
        for mutation in config["mutants"]:
            if not mutation["test_ids"] or not set(mutation["test_ids"]).issubset(config["test_ids"]):
                raise ValueError("mutation tests must be nonempty members of passing baseline")
            mutation_baseline = execute_fixture(source, config, mutation["test_ids"])
            mutation_baseline.pop("coverage", None)
            if mutation_baseline["status"] != "pass":
                report["mutants"].append({"id": mutation["id"], "target": mutation["target"],
                                          "status": "baseline_failed", "baseline": mutation_baseline})
                report["failures"].append(f"mutation baseline failed: {mutation['id']}")
                continue
            result = execute_fixture(source, config, mutation["test_ids"], mutation)
            report["mutants"].append({"id": mutation["id"], "target": mutation["target"],
                                      "baseline": mutation_baseline, **result})
            if result["status"] != "killed":
                report["failures"].append(f"mutant not assertion-killed: {mutation['id']}")
        completed = True
    except Exception as error:
        # Infrastructure/dependency/report-format exceptions must never emit a PASS artifact.
        report["failures"].append(f"{type(error).__name__}: {error}")
    finally:
        try:
            report["original_unchanged"] = bool(before) and all(
                (source / name).is_file() and digest(source / name) == value for name, value in before.items())
        except OSError:
            report["original_unchanged"] = False
        if not report["original_unchanged"]:
            report["failures"].append("original source changed during verification")
        if not completed and not report["failures"]:
            report["failures"].append("run incomplete")
        report["status"] = "pass" if completed and not report["failures"] else "failed"
        (output / "quality.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        lines = [f"quality={report['status']}", f"original_unchanged={report['original_unchanged']}"]
        for name, data in report.get("coverage", {}).items():
            lines.extend([f"{name}: statement={data['statement_percent']} branch={data['branch_percent']}",
                          f"  counts={data['summary']}",
                          f"  executed_lines={data.get('executed_lines')}",
                          f"  executed_arcs={data.get('executed_branches')}",
                          f"  missing_lines={data.get('missing_lines')}",
                          f"  missing_arcs={data.get('missing_branches')}"])
            for function, detail in data.get("critical_functions", {}).items():
                lines.append(f"  critical {function}: statement={detail['statement_percent']} "
                             f"branch={detail['branch_percent']} missing_arcs={detail.get('missing_branches')}")
        lines.extend(f"mutant {item['id']}: {item['status']}" for item in report["mutants"])
        lines.extend(report["failures"])
        (output / "quality.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if report["status"] == "pass" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/verification-quality")
    parser.add_argument("--_worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._worker:
        return worker(args._worker, json.loads((args._worker / "worker-request.json").read_text()))
    # Invalidate stale success before reading configuration: even absent/malformed
    # input must leave a failed evidence record for this invocation.
    args.output_dir.mkdir(parents=True, exist_ok=True)
    startup = {"schema_version": 1, "status": "failed", "failures": ["configuration load incomplete"]}
    (args.output_dir / "quality.json").write_text(json.dumps(startup, indent=2), encoding="utf-8")
    (args.output_dir / "quality.txt").write_text("quality=failed\nconfiguration load incomplete\n", encoding="utf-8")
    try:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("quality configuration must be a JSON object")
    except (OSError, ValueError) as error:
        startup["failures"] = [f"configuration load failed: {type(error).__name__}"]
        (args.output_dir / "quality.json").write_text(json.dumps(startup, indent=2), encoding="utf-8")
        (args.output_dir / "quality.txt").write_text(
            "quality=failed\n" + startup["failures"][0] + "\n", encoding="utf-8")
        return 1
    return run_quality(ROOT, config, args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
