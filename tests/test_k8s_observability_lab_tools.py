"""Behavior tests for the isolated Loki lab operator entrypoint."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/k8s/tools/observability-lab.sh"


class ObservabilityLabToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.calls = self.directory / "calls"
        fake = self.directory / "sudo"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$LAB_CALLS\"\n"
            "case \"$*\" in\n"
            "  *'config current-context'*) printf '%s\\n' \"${LAB_CONTEXT:-k3s-test}\";;\n"
            "  *'get namespace observability-lab'*) if [ \"${LAB_NS_EXISTS:-yes}\" = yes ] && [[ \"$*\" == *'-o name'* ]]; then printf 'namespace/observability-lab'; fi;;\n"
            "  *'get pvc loki-lab-data'*) printf Bound;;\n"
            "  *'get --raw '*'/query_range?'*) count=$(awk '/query_range/ {n++} END {print n}' \"$LAB_CALLS\"); if [ \"${LAB_EMPTY_QUERY:-no}\" = yes ] || [ \"$count\" -le \"${LAB_DELAY_QUERY:-0}\" ]; then printf '%s\\n' '{\"status\":\"success\",\"data\":{\"result\":[]}}'; else printf '%s\\n' '{\"status\":\"success\",\"data\":{\"result\":[{\"values\":[[\"1\",\"loki lab sample ready\"]]}]}}'; fi;;\n"
            "  *'get --raw '*'/ready'*) count=$(awk '/proxy\\/ready/ {n++} END {print n}' \"$LAB_CALLS\"); [ \"${LAB_FAIL_READY:-no}\" = no ] && [ \"$count\" -gt \"${LAB_DELAY_READY:-0}\" ];;\n"
            "  *'--dry-run=server'*) [ \"${LAB_FAIL_DRY_RUN:-no}\" = no ];;\n"
            "  *) exit 0;;\n"
            "esac\n"
        )
        fake.chmod(0o755)
        sleeper = self.directory / "sleep"
        sleeper.write_text("#!/bin/sh\nexit 0\n")
        sleeper.chmod(0o755)

    def run_tool(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(PATH=f"{self.directory}:{env['PATH']}", LAB_CALLS=str(self.calls))
        env.update(extra_env)
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def recorded(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_missing_mode_fails_without_contacting_cluster(self) -> None:
        result = self.run_tool()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.recorded())

    def test_context_mismatch_blocks_all_writes(self) -> None:
        result = self.run_tool("--apply", "--context", "wrong", LAB_CONTEXT="k3s-test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("observability_lab=FAIL", result.stdout)
        self.assertFalse(any(" apply " in call or " delete " in call for call in self.recorded()))

    def test_check_is_read_only_and_checks_prerequisites(self) -> None:
        result = self.run_tool("--check", "--context", "k3s-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("observability_lab=PASS", result.stdout)
        calls = self.recorded()
        self.assertTrue(any("get namespace observability-lab" in call for call in calls))
        self.assertTrue(any("get storageclass local-path" in call for call in calls))
        self.assertTrue(any("get deployment personal-server-monitoring-grafana" in call for call in calls))
        self.assertFalse(any(" apply " in call or " delete " in call or " create " in call for call in calls))

    def test_apply_dry_runs_before_each_write(self) -> None:
        result = self.run_tool("--apply", "--context", "k3s-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded()
        dry_runs = [i for i, call in enumerate(calls) if "--dry-run=server" in call]
        writes = [i for i, call in enumerate(calls) if " apply " in call and "--dry-run=server" not in call]
        self.assertTrue(dry_runs)
        self.assertTrue(writes)
        self.assertLess(dry_runs[0], writes[0])
        self.assertLess(dry_runs[-1], writes[-1])
        self.assertFalse(any("monitoring-install" in call or "monitoring-uninstall" in call for call in calls))

    def test_failed_dry_run_prevents_writes(self) -> None:
        result = self.run_tool("--apply", "--context", "k3s-test", LAB_FAIL_DRY_RUN="yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("observability_lab=FAIL", result.stdout)
        self.assertFalse(any(" apply " in call and "--dry-run=server" not in call for call in self.recorded()))

    def test_first_apply_creates_only_fixed_namespace_after_server_dry_run(self) -> None:
        result = self.run_tool("--apply", "--context", "k3s-test", LAB_NS_EXISTS="no")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded()
        dry_run = next(i for i, call in enumerate(calls) if "create namespace observability-lab --dry-run=server" in call)
        create = next(i for i, call in enumerate(calls) if "create namespace observability-lab" in call and "--dry-run=server" not in call)
        self.assertLess(dry_run, create)
        self.assertFalse(any("create namespace" in call and "observability-lab" not in call for call in calls))

    def test_verify_checks_ready_and_fixed_sample_selector(self) -> None:
        result = self.run_tool("--verify", "--context", "k3s-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded()
        self.assertTrue(any("/ready" in call for call in calls))
        self.assertTrue(any("/query_range?" in call and "loki-lab-sample" in call for call in calls))
        self.assertTrue(any("wait --for=condition=Available deployment/loki-lab" in call for call in calls))
        self.assertFalse(any(" apply " in call or " delete " in call for call in calls))

    def test_verify_rejects_empty_sample_logs_without_printing_query_data(self) -> None:
        result = self.run_tool("--verify", "--context", "k3s-test", LAB_EMPTY_QUERY="yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("observability_lab=FAIL", result.stdout)
        self.assertNotIn('"result"', result.stdout + result.stderr)

    def test_delayed_sample_logs_succeed_on_third_attempt(self) -> None:
        result = self.run_tool("--verify", "--context", "k3s-test", LAB_DELAY_QUERY="2")
        self.assertEqual(result.returncode, 0, result.stderr)
        queries = [call for call in self.recorded() if "/query_range?" in call]
        self.assertEqual(len(queries), 3)
        self.assertTrue(all("--request-timeout=15s" in call for call in queries))

    def test_persistent_readiness_failure_stops_after_three_attempts(self) -> None:
        result = self.run_tool("--verify", "--context", "k3s-test", LAB_FAIL_READY="yes")
        self.assertNotEqual(result.returncode, 0)
        ready = [call for call in self.recorded() if "/ready" in call]
        self.assertEqual(len(ready), 3)
        self.assertTrue(all("--request-timeout=15s" in call for call in ready))
        self.assertFalse(any("query_range" in call for call in self.recorded()))

    def test_delayed_readiness_recovers_and_queries_after_ready(self) -> None:
        result = self.run_tool("--verify", "--context", "k3s-test", LAB_DELAY_READY="2")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded()
        ready = [i for i, call in enumerate(calls) if "/ready" in call]
        query = [i for i, call in enumerate(calls) if "query_range" in call]
        self.assertEqual(len(ready), 3)
        self.assertLess(ready[-1], query[0])

    def test_empty_sample_query_stops_after_three_attempts(self) -> None:
        result = self.run_tool("--verify", "--context", "k3s-test", LAB_EMPTY_QUERY="yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sum("query_range" in call for call in self.recorded()), 3)

    def test_rollback_uses_only_named_resources_and_keeps_pvc(self) -> None:
        result = self.run_tool("--rollback", "--context", "k3s-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded()
        self.assertTrue(any("delete configmap loki-lab-datasource" in call for call in calls))
        self.assertTrue(any("delete configmap loki-lab-dashboard" in call for call in calls))
        self.assertTrue(any("delete deployment loki-lab" in call for call in calls))
        self.assertFalse(any("delete pvc" in call or "delete namespace" in call or "--all" in call for call in calls))

    def test_delete_data_only_removes_named_lab_pvc(self) -> None:
        result = self.run_tool("--rollback", "--delete-data", "--context", "k3s-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded()
        self.assertEqual(sum("delete pvc loki-lab-data" in call for call in calls), 1)
        self.assertFalse(any("delete namespace" in call or "--all" in call for call in calls))

    def test_delete_data_rejected_without_rollback(self) -> None:
        result = self.run_tool("--check", "--delete-data", "--context", "k3s-test")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.recorded())


if __name__ == "__main__":
    unittest.main()
