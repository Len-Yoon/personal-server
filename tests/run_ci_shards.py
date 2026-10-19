"""Run the unchanged K8s unittest modules in isolated, coverage-checked workers."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.ci_test_results import RecordedResult, cases, inventory, outcome, verify_coverage, write_evidence


def collect(modules):
    loader = unittest.TestLoader()
    loaded = loader.loadTestsFromNames(modules)
    return loaded, inventory(loaded, loader)


def partition(modules, count):
    # Whole modules stay together so module/class fixtures retain their semantics.
    groups = [[] for _ in range(count)]
    sizes = [0] * count
    inventory = {}
    for module in modules:
        _, inventory[module] = collect([module])
    timings = json.loads((ROOT / "tests/k8s_shard_timings.json").read_text())["module_seconds"]
    if not isinstance(timings, dict) or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in timings.values()):
        raise ValueError("invalid shard scheduling estimates")
    weights = {module: max(0.001, timings.get(module, len(inventory[module]))) for module in modules}
    for module in sorted(modules, key=lambda name: (-weights[name], name)):
        index = min(range(count), key=lambda slot: sizes[slot])
        groups[index].append(module)
        sizes[index] += weights[module]
    return [group for group in groups if group], inventory


def worker(modules, output, preload=None, fail_on_skip=False):
    # The serial runner imports every module before executing any test. Preserve
    # that import context (some legacy tool tests register sibling imports).
    if preload:
        collect(preload)
    suite, expected = collect(modules)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=1, resultclass=RecordedResult).run(suite)
    report = outcome(result, expected, fail_on_skip)
    report.update(modules=modules, seconds=time.monotonic() - started, fail_on_skip=fail_on_skip)
    write_evidence(output, report)
    return 0 if report["success"] else 1


def run(modules, count, evidence, fail_on_skip=False):
    _, expected = collect(modules)
    groups, _ = partition(modules, count)
    started = time.monotonic()
    reports = []
    with tempfile.TemporaryDirectory(prefix="ci-shards-") as scratch:
        def launch(pair):
            index, group = pair
            output = Path(scratch) / f"{index}.json"
            command = [sys.executable, str(Path(__file__).resolve()), "--worker-output", str(output)]
            if fail_on_skip:
                command.append("--fail-on-skip")
            result = subprocess.run([*command, *group], cwd=ROOT)
            try:
                report = json.loads(output.read_text())
                if not isinstance(report, dict):
                    raise ValueError("worker report must be an object")
                if not isinstance(report.get("executed_ids"), list) or any(not isinstance(item, str) for item in report["executed_ids"]):
                    raise ValueError("worker report has invalid executed IDs")
                for key in ("skipped", "expected_failures", "unexpected_successes", "failures", "errors"):
                    entries = report.get(key, [])
                    if not isinstance(entries, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("reason"), str) for item in entries):
                        raise ValueError(f"worker report has invalid {key}")
            except (OSError, ValueError) as error:
                report = {"success": False, "report_error": str(error)}
            return result.returncode, report
        with ThreadPoolExecutor(max_workers=count) as pool:
            reports = list(pool.map(launch, enumerate(groups)))
    observed = [case for _, report in reports for case in report.get("executed_ids", [])]
    coverage_error = None
    try:
        verify_coverage(expected, observed)
    except ValueError as error:
        coverage_error = str(error)
    anomalies = {key: [item for _, report in reports for item in report.get(key, [])]
                 for key in ("skipped", "expected_failures", "unexpected_successes", "failures", "errors")}
    success = coverage_error is None and all(code == 0 and report.get("success") is True for code, report in reports)
    success = success and not (fail_on_skip and (anomalies["skipped"] or anomalies["expected_failures"] or anomalies["unexpected_successes"]))
    result = {"success": success, "expected_ids": expected, "executed_ids": observed,
              "seconds": time.monotonic() - started, "coverage_error": coverage_error,
              "workers": [report for _, report in reports], "fail_on_skip": fail_on_skip, **anomalies}
    write_evidence(evidence, result)
    print(f"K8s shards: {len(observed)} cases, {len(groups)} workers, "
          f"{result['seconds']:.2f}s, success={success}")
    return 0 if success else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, choices=(1, 2, 4), default=4)
    parser.add_argument("--evidence")
    parser.add_argument("--worker-output")
    parser.add_argument("--fail-on-skip", action="store_true")
    parser.add_argument("modules", nargs="*")
    args = parser.parse_args()
    try:
        from tests.run_service_tests import load_matrix, validate_matrix_coverage
        matrix = load_matrix()
        validate_matrix_coverage(matrix)
        import shlex
        entry = next(item for item in matrix if item["name"] == "k8s-contracts")
        modules = shlex.split(entry["test_command"])[3:]
        if args.worker_output:
            if not args.modules or not set(args.modules) <= set(modules):
                raise ValueError("unexpected worker modules")
            return worker(args.modules, args.worker_output, modules, args.fail_on_skip)
        return run(modules, args.workers, args.evidence, args.fail_on_skip)
    except (ValueError, OSError) as error:
        print(f"K8s shard verification failed: {error}", file=sys.stderr)
        write_evidence(args.worker_output or args.evidence, {"success": False, "collection_error": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
