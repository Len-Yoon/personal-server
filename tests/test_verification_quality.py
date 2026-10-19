"""Failure injection for the bounded quality gate; no coverage dependency needed."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("verification_quality", ROOT / "scripts/run_verification_quality.py")
quality = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quality)


def passing(test_ids):
    return {"test_ids": test_ids, "run": len(test_ids), "errors": 0, "skipped": 0,
            "failures": 0, "expected_failures": 0, "unexpected_successes": 0}


class VerificationQualityTests(unittest.TestCase):
    def test_assertion_kill_is_distinct_from_error_skip_empty_or_survival(self):
        good = passing(["existing.test"])
        self.assertTrue(quality.acceptable_result(good, ["existing.test"], mutant=False))
        self.assertFalse(quality.acceptable_result(good, ["existing.test"], mutant=True))
        killed = {**good, "failures": 1}
        self.assertTrue(quality.acceptable_result(killed, ["existing.test"], mutant=True))
        for override in ({"errors": 1}, {"skipped": 1}, {"run": 0},
                         {"test_ids": ["unittest.loader._FailedTest.import_error"]},
                         {"expected_failures": 1}, {"unexpected_successes": 1}):
            self.assertFalse(quality.acceptable_result({**killed, **override}, ["existing.test"], mutant=True))
        self.assertFalse(quality.acceptable_result(passing([]), [], mutant=False))
        self.assertFalse(quality.acceptable_result([], ["existing.test"], mutant=False))
        self.assertFalse(quality.acceptable_result({**good, "failures": "1"}, ["existing.test"], mutant=True))
        self.assertEqual(quality.execution_status(good, ["existing.test"], 0, mutant=True), "survived")
        self.assertEqual(quality.execution_status(killed, ["existing.test"], 1, mutant=True), "killed")
        self.assertEqual(quality.execution_status({**killed, "errors": 1}, ["existing.test"], 1, mutant=True), "invalid")
        self.assertEqual(quality.execution_status(killed, ["existing.test"], 2, mutant=True), "invalid")

    def test_copy_is_exact_code_allowlist_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "source", root / "copy"
            for relative in quality.COPY_ALLOWLIST:
                file = source / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("# fixture code\n")
            (source / "data").mkdir()
            (source / "data/credential.txt").write_text("must never copy")
            hashes = quality.copy_fixture(source, destination)
            copied = {str(p.relative_to(destination)) for p in destination.rglob("*") if p.is_file()}
            self.assertEqual(copied, set(quality.COPY_ALLOWLIST))
            self.assertEqual(set(hashes), copied)
            selected = source / quality.COPY_ALLOWLIST[0]
            selected.unlink()
            selected.symlink_to(source / quality.COPY_ALLOWLIST[1])
            with self.assertRaisesRegex(ValueError, "symlink"):
                quality.copy_fixture(source, root / "rejected")

    def test_environment_drops_credentials_proxy_and_original_paths(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "AWS_SECRET_ACCESS_KEY": "sentinel", "HTTP_PROXY": "sentinel",
            "FILE_STORAGE_PATH": "/production/data", "PYTHONPATH": "/original/repo",
        }):
            root = Path(directory)
            env = quality.fixture_environment(root)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
            self.assertNotIn("HTTP_PROXY", env)
            self.assertNotIn("PYTHONPATH", env)
            for name in ("HOME", "TMPDIR", "FILE_STORAGE_PATH", "AUTH_RATE_LIMIT_STATE_PATH", "SECURITY_LOG_PATH"):
                self.assertTrue(Path(env[name]).is_relative_to(root))

    def test_mutation_requires_unique_real_change_and_fixed_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / quality.TARGETS[0]
            target.parent.mkdir(parents=True)
            target.write_text("guard >= limit\n")
            mutation = {"target": quality.TARGETS[0], "before": "guard >= limit", "after": "guard > limit"}
            quality.apply_mutant(root, mutation)
            self.assertEqual(target.read_text(), "guard > limit\n")
            with self.assertRaises(ValueError):
                quality.apply_mutant(root, mutation)
            with self.assertRaises(ValueError):
                quality.apply_mutant(root, {**mutation, "target": "../../outside"})
            with self.assertRaises(ValueError):
                quality.apply_mutant(root, {**mutation, "before": "guard > limit", "after": "guard > limit"})

    def test_coverage_rejects_missing_target_line_only_zero_branch_and_regression(self):
        entry = {"summary": {"num_statements": 10, "covered_lines": 8,
                              "num_branches": 4, "covered_branches": 3}}
        floors = {"production.py": {"statement_min": 80, "branch_min": 75}}
        report = {"meta": {"branch_coverage": True}, "files": {"production.py": entry}}
        self.assertEqual(quality.coverage_verdict(report, floors)[1], [])
        cases = [
            {**report, "files": {}},
            {**report, "meta": {"branch_coverage": False}},
            {**report, "files": {"production.py": {"summary": {**entry["summary"], "num_branches": 0}}}},
            {**report, "files": {"production.py": {"summary": {**entry["summary"], "covered_branches": 2}}}},
            {**report, "files": {"production.py": {"summary": {**entry["summary"], "covered_lines": 7}}}},
        ]
        for case in cases:
            self.assertTrue(quality.coverage_verdict(case, floors)[1])
        floors["production.py"]["functions"] = {"critical": {"statement_min": 80, "branch_min": 75}}
        self.assertTrue(quality.coverage_verdict(report, floors)[1], "missing critical function must fail")

    def test_survivor_or_failed_baseline_closes_gate_and_preserves_report(self):
        config = json.loads((ROOT / "tests/verification_quality.json").read_text())
        baseline = {"status": "pass", "coverage": {"meta": {"branch_coverage": True}}}
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict("sys.modules", {"coverage": SimpleNamespace(__version__=config["coverage_version"])}), \
                patch.object(quality, "coverage_verdict", return_value=({}, [])):
            with patch.object(quality, "execute_fixture", side_effect=[
                baseline.copy(), {"status": "pass"}, {"status": "failed"},
                {"status": "pass"}, {"status": "killed"}, {"status": "pass"}, {"status": "killed"},
            ]):
                self.assertEqual(quality.run_quality(ROOT, config, Path(directory)), 1)
            report = json.loads((Path(directory) / "quality.json").read_text())
            self.assertIn("mutant not assertion-killed", report["failures"][0])
            self.assertTrue(report["original_unchanged"])
            with patch.object(quality, "execute_fixture", return_value={"status": "failed"}) as execute:
                self.assertEqual(quality.run_quality(ROOT, config, Path(directory)), 1)
                self.assertEqual(execute.call_count, 1, "failed baseline must prevent mutation execution")

    def test_committed_suite_has_fixed_production_targets_and_positive_floors(self):
        config = json.loads((ROOT / "tests/verification_quality.json").read_text())
        self.assertEqual(set(config["targets"]), set(quality.TARGETS))
        self.assertEqual({item["target"] for item in config["mutants"]}, set(quality.TARGETS))
        for floors in config["targets"].values():
            self.assertGreater(floors["statement_min"], 0)
            self.assertGreater(floors["branch_min"], 0)
            self.assertTrue(floors["functions"])

    def test_invalid_numeric_floors_never_disable_the_gate(self):
        for invalid in (0, -1, 101, float("nan"), float("inf"), True, "50"):
            config = json.loads((ROOT / "tests/verification_quality.json").read_text())
            config["targets"][quality.TARGETS[0]]["branch_min"] = invalid
            with self.assertRaises(ValueError):
                quality.validate_config(config)

    def test_exact_missing_arcs_detect_regression_even_above_percentage_floor(self):
        name = "production.py"
        entry = {"summary": {"num_statements": 388, "covered_lines": 387,
                             "num_branches": 158, "covered_branches": 157},
                 "missing_lines": [471], "missing_branches": [[465, 471]]}
        floors = {name: {"statement_min": 99, "branch_min": 99,
                        "baseline_counts": {"num_statements": 388, "num_branches": 158},
                        "expected_missing_lines": [471],
                        "expected_missing_branches": [[465, 471]]}}
        report = {"meta": {"branch_coverage": True}, "files": {name: entry}}
        self.assertEqual(quality.coverage_verdict(report, floors)[1], [])
        for change in ({"missing_lines": [470]}, {"missing_branches": [[465, 470]]},
                       {"missing_lines": []}, {"missing_branches": []}):
            with self.subTest(change=change):
                report["files"][name] = {**entry, **change}
                self.assertTrue(quality.coverage_verdict(report, floors)[1])

    def test_exclusions_and_denominator_changes_cannot_manufacture_full_coverage(self):
        name = "production.py"
        entry = {"summary": {"num_statements": 2, "covered_lines": 2,
                             "num_branches": 2, "covered_branches": 2},
                 "excluded_lines": [], "missing_lines": [], "missing_branches": []}
        floors = {name: {"statement_min": 100, "branch_min": 100,
                        "baseline_counts": {"num_statements": 2, "num_branches": 2},
                        "expected_missing_lines": [], "expected_missing_branches": []}}
        report = {"meta": {"branch_coverage": True}, "files": {name: entry}}
        self.assertEqual(quality.coverage_verdict(report, floors)[1], [])
        for change in ({"excluded_lines": [3]},
                       {"summary": {**entry["summary"], "num_statements": 1, "covered_lines": 1}},
                       {"summary": {**entry["summary"], "num_branches": 1, "covered_branches": 1}}):
            with self.subTest(change=change):
                report["files"][name] = {**entry, **change}
                self.assertTrue(quality.coverage_verdict(report, floors)[1])

    def test_missing_path_contracts_require_lists_and_source_bound_proofs(self):
        for key, value in (("expected_missing_lines", None), ("expected_missing_lines", [True]),
                           ("expected_missing_lines", [471, 471]),
                           ("expected_missing_branches", [[1]]),
                           ("expected_missing_branches", [[1, 2], [1, 2]]),
                           ("unreachable_proof", {}),
                           ("unreachable_proof", {"reason": "unreachable", "source_sha256": "x" * 64})):
            config = json.loads((ROOT / "tests/verification_quality.json").read_text())
            config["targets"][quality.TARGETS[1]][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                quality.validate_config(config)

    def test_stale_unreachable_source_proof_blocks_before_execution(self):
        config = json.loads((ROOT / "tests/verification_quality.json").read_text())
        config["targets"][quality.TARGETS[1]]["unreachable_proof"]["source_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict("sys.modules", {"coverage": SimpleNamespace(__version__=config["coverage_version"])}), \
                patch.object(quality, "execute_fixture") as execute:
            self.assertEqual(quality.run_quality(ROOT, config, Path(directory)), 1)
            execute.assert_not_called()
            report = json.loads((Path(directory) / "quality.json").read_text())
            self.assertIn("unreachable source proof is stale", report["failures"][0])

    def test_dependency_and_report_exceptions_always_write_failed_artifact(self):
        config = json.loads((ROOT / "tests/verification_quality.json").read_text())
        for exception in (ImportError("missing dependency"), TypeError("bad worker record"), RuntimeError("coverage API")):
            with tempfile.TemporaryDirectory() as directory, \
                    patch.dict("sys.modules", {"coverage": SimpleNamespace(__version__=config["coverage_version"])}), \
                    patch.object(quality, "execute_fixture", side_effect=exception):
                output = Path(directory)
                (output / "quality.json").write_text('{"status":"pass"}')
                self.assertEqual(quality.run_quality(ROOT, config, output), 1)
                report = json.loads((output / "quality.json").read_text())
                self.assertEqual(report["status"], "failed")
                self.assertTrue(report["failures"])

    def test_timeout_kills_process_group_and_never_counts_as_a_kill(self):
        config = json.loads((ROOT / "tests/verification_quality.json").read_text())
        process = SimpleNamespace(pid=12345, communicate=unittest.mock.Mock(side_effect=[
            subprocess.TimeoutExpired("worker", 1), ("", None)]))
        with patch.object(quality, "copy_fixture", return_value={"source": "hash"}), \
                patch.object(quality.subprocess, "Popen", return_value=process), \
                patch.object(quality.os, "killpg") as kill:
            result = quality.execute_fixture(ROOT, config, ["existing.test"])
            self.assertEqual(result["status"], "timeout")
            kill.assert_called_once_with(12345, quality.signal.SIGKILL)

    def test_original_hash_change_fails_even_when_all_checks_pass(self):
        config = json.loads((ROOT / "tests/verification_quality.json").read_text())
        baseline = {"status": "pass", "coverage": {}}
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict("sys.modules", {"coverage": SimpleNamespace(__version__=config["coverage_version"])}), \
                patch.object(quality, "coverage_verdict", return_value=({}, [])), \
                patch.object(quality, "execute_fixture", side_effect=[
                    baseline.copy(), {"status": "pass"}, {"status": "killed"},
                    {"status": "pass"}, {"status": "killed"}, {"status": "pass"}, {"status": "killed"},
                ]), patch.object(quality, "digest", side_effect=["original"] * len(quality.COPY_ALLOWLIST) + [
                    config["targets"][quality.TARGETS[1]]["unreachable_proof"]["source_sha256"], "changed"]):
            self.assertEqual(quality.run_quality(ROOT, config, Path(directory)), 1)
            report = json.loads((Path(directory) / "quality.json").read_text())
            self.assertFalse(report["original_unchanged"])
            self.assertIn("original source changed", report["failures"][0])

    def test_cli_config_failure_replaces_stale_success_before_running_tests(self):
        for content in ("{malformed", "null", "[]", None):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / "config.json"
                if content is not None:
                    config.write_text(content)
                output = root / "output"
                output.mkdir()
                (output / "quality.json").write_text('{"status":"pass"}')
                (output / "quality.txt").write_text("quality=pass\n")
                with patch.object(quality, "CONFIG", config), \
                        patch("sys.argv", ["quality", "--output-dir", str(output)]), \
                        patch.object(quality, "run_quality") as run:
                    self.assertEqual(quality.main(), 1)
                    run.assert_not_called()
                report = json.loads((output / "quality.json").read_text())
                self.assertEqual(report["status"], "failed")
                self.assertIn("configuration load failed", report["failures"][0])
                self.assertIn("quality=failed", (output / "quality.txt").read_text())

    def test_direct_nonobject_config_also_invalidates_stale_success(self):
        for config in (None, []):
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                (output / "quality.json").write_text('{"status":"pass"}')
                self.assertEqual(quality.run_quality(ROOT, config, output), 1)
                self.assertEqual(json.loads((output / "quality.json").read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
