"""Small unittest evidence contract shared by the CI runners."""
from collections import Counter
import json
from pathlib import Path
import unittest


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def inventory(suite, loader):
    if loader.errors:
        raise ValueError("unittest collection failed: " + "\n".join(loader.errors))
    ids = [case.id() for case in cases(suite)]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("test collection is empty or contains duplicate case IDs")
    return ids


def verify_coverage(expected, observed):
    if not expected or len(expected) != len(set(expected)) or Counter(expected) != Counter(observed) or len(observed) != len(set(observed)):
        raise ValueError("shard case coverage is empty, missing, duplicated or unexpected")


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executed = []

    def startTest(self, test):
        self.executed.append(test.id())
        super().startTest(test)


def outcome(result, expected, fail_on_skip=False):
    coverage_error = None
    try:
        verify_coverage(expected, result.executed)
    except ValueError as error:
        coverage_error = str(error)
    skipped = [{"id": test.id(), "reason": reason} for test, reason in result.skipped]
    expected_failures = [{"id": test.id(), "reason": reason} for test, reason in result.expectedFailures]
    unexpected_successes = [{"id": test.id(), "reason": "unexpected success"} for test in result.unexpectedSuccesses]
    return {
        "success": result.wasSuccessful() and coverage_error is None and not (fail_on_skip and (skipped or expected_failures)),
        "expected_ids": expected, "executed_ids": result.executed,
        "tests_run": result.testsRun, "coverage_error": coverage_error,
        "skipped": skipped, "expected_failures": expected_failures,
        "unexpected_successes": unexpected_successes,
        "failures": [{"id": test.id(), "reason": reason} for test, reason in result.failures],
        "errors": [{"id": test.id(), "reason": reason} for test, reason in result.errors],
    }


def write_evidence(path, report):
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2), encoding="utf-8")
