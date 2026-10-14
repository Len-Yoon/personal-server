"""Run the unchanged K8s unittest modules in isolated, coverage-checked workers."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def collect(modules):
    loader = unittest.TestLoader()
    loaded = loader.loadTestsFromNames(modules)
    if loader.errors:
        raise ValueError("unittest collection failed: " + "\n".join(loader.errors))
    ids = [case.id() for case in cases(loaded)]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("test collection is empty or contains duplicate case IDs")
    return loaded, ids


def partition(modules, count):
    # Whole modules stay together so module/class fixtures retain their semantics.
    groups = [[] for _ in range(count)]
    sizes = [0] * count
    inventory = {}
    for module in modules:
        _, inventory[module] = collect([module])
    for module in sorted(modules, key=lambda name: (-len(inventory[name]), name)):
        index = min(range(count), key=lambda slot: sizes[slot])
        groups[index].append(module)
        sizes[index] += len(inventory[module])
    return [group for group in groups if group], inventory


def verify_coverage(expected, observed):
    if Counter(expected) != Counter(observed) or len(observed) != len(set(observed)):
        raise ValueError("shard case coverage is missing, duplicated or unexpected")


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executed = []

    def startTest(self, test):
        self.executed.append(test.id())
        super().startTest(test)


def worker(modules, output, preload=None):
    # The serial runner imports every module before executing any test. Preserve
    # that import context (some legacy tool tests register sibling imports).
    if preload:
        collect(preload)
    suite, expected = collect(modules)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=1, resultclass=RecordedResult).run(suite)
    # A failing module fixture must fail even when it prevented test startTest calls.
    success = result.wasSuccessful()
    try:
        verify_coverage(expected, result.executed)
    except ValueError:
        success = False
    Path(output).write_text(json.dumps({
        "modules": modules, "expected_ids": expected, "executed_ids": result.executed,
        "success": success, "seconds": time.monotonic() - started,
    }), encoding="utf-8")
    return 0 if success else 1


def run(modules, count, evidence):
    _, expected = collect(modules)
    groups, _ = partition(modules, count)
    started = time.monotonic()
    reports = []
    with tempfile.TemporaryDirectory(prefix="ci-shards-") as scratch:
        def launch(pair):
            index, group = pair
            output = Path(scratch) / f"{index}.json"
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                     "--worker-output", str(output), *group], cwd=ROOT)
            report = json.loads(output.read_text()) if output.exists() else {}
            return result.returncode, report
        with ThreadPoolExecutor(max_workers=count) as pool:
            reports = list(pool.map(launch, enumerate(groups)))
    observed = [case for _, report in reports for case in report.get("executed_ids", [])]
    coverage_error = None
    try:
        verify_coverage(expected, observed)
    except ValueError as error:
        coverage_error = str(error)
    success = coverage_error is None and all(code == 0 and report.get("success") is True for code, report in reports)
    result = {"success": success, "expected_ids": expected, "executed_ids": observed,
              "seconds": time.monotonic() - started, "coverage_error": coverage_error,
              "workers": [report for _, report in reports]}
    if evidence:
        Path(evidence).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"K8s shards: {len(observed)} cases, {len(groups)} workers, "
          f"{result['seconds']:.2f}s, success={success}")
    return 0 if success else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, choices=(1, 2, 4), default=4)
    parser.add_argument("--evidence")
    parser.add_argument("--worker-output")
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
            return worker(args.modules, args.worker_output, modules)
        return run(modules, args.workers, args.evidence)
    except (ValueError, OSError) as error:
        print(f"K8s shard verification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
