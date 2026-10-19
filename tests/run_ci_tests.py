"""Execute a selected CI matrix suite with explicit unittest outcome evidence."""
import argparse
import shlex
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.ci_test_results import RecordedResult, inventory, outcome, write_evidence
from tests.run_service_tests import DOCUMENTATION_COMMAND, files_for_test_command, load_matrix, validate_matrix_coverage


def collect(command):
    files_for_test_command(command)
    targets = shlex.split(command)[3:]
    loader = unittest.TestLoader()
    if targets[0] == "discover":
        # Match `unittest discover -s ...`: top-level modules and fixture scope.
        suite = loader.discover(str(ROOT / targets[2]))
    else:
        suite = loader.loadTestsFromNames(targets)
    return suite, inventory(suite, loader)


def run(name, evidence, fail_on_skip=False):
    started = time.monotonic()
    report = {"suite": name, "success": False}
    original_path = sys.path[:]
    try:
        matrix = load_matrix()
        validate_matrix_coverage(matrix)
        if name == "documentation":
            entry = {"test_command": DOCUMENTATION_COMMAND, "pythonpath": "."}
        else:
            entry = next((item for item in matrix if item["name"] == name), None)
            if entry is None:
                raise ValueError(f"unknown CI suite: {name}")
        sys.path.insert(1, str(ROOT / entry["pythonpath"]))
        suite, expected = collect(entry["test_command"])
        result = unittest.TextTestRunner(verbosity=1, resultclass=RecordedResult).run(suite)
        report.update(outcome(result, expected, fail_on_skip))
    except (ValueError, OSError, ImportError) as error:
        report["collection_error"] = str(error)
    finally:
        sys.path[:] = original_path
    report["seconds"] = time.monotonic() - started
    report["fail_on_skip"] = fail_on_skip
    write_evidence(evidence, report)
    return 0 if report["success"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--fail-on-skip", action="store_true")
    args = parser.parse_args()
    return run(args.suite, args.evidence, args.fail_on_skip)


if __name__ == "__main__":
    raise SystemExit(main())
