import subprocess
import sys
import unittest
from pathlib import Path

from tests.run_service_tests import SUITES


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tests" / "run_service_tests.py"


class ServiceTestRunnerTests(unittest.TestCase):
    def test_runner_executes_selected_service_with_its_own_import_path(self):
        """Fails if a service suite no longer receives its isolated PYTHONPATH."""
        result = subprocess.run(
            [sys.executable, str(RUNNER), "--suite", "system-agent"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[PASS] system-agent", result.stdout)

    def test_crawler_worker_suite_executes_news_auto_refresh_client_test(self):
        """Fails if crawler-worker omits its browser-side time display regression test."""
        result = subprocess.run(
            [sys.executable, str(RUNNER), "--suite", "crawler-worker"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("자동 새로고침은 UTC 기사 시각을 KST 분 단위로 표시한다", result.stdout)

    def test_maintenance_suite_runs_documentation_index_checks(self):
        """Fails if the local maintenance runner omits documentation index validation."""
        maintenance = next(suite for suite in SUITES if suite.name == "maintenance")

        self.assertIn("tests.test_documentation_index", maintenance.command)
        self.assertEqual(maintenance.command.count("tests.test_documentation_index"), 1)

    def test_k8s_contract_suite_runs_every_k8s_contract_module(self):
        suite = next(item for item in SUITES if item.name == "k8s-contracts")
        expected_modules = (
            "tests.test_k8s_memo_crawler_workload_templates",
            "tests.test_k8s_monitoring_tools",
            "tests.test_k8s_monitoring_values",
            "tests.test_k8s_portal_availability_alert",
            "tests.test_k8s_portal_backup_verify",
            "tests.test_k8s_portal_cutover",
            "tests.test_k8s_portal_nodeport_connectivity_smoke",
            "tests.test_k8s_portal_pvc_backup_automation",
            "tests.test_k8s_portal_pvc_backup_verify",
            "tests.test_k8s_portal_secret_shadow_smoke",
            "tests.test_k8s_sre_health_audit",
            "tests.test_k8s_sre_pod_recovery_lab",
            "tests.test_k8s_sre_telegram_manifests",
            "tests.test_k8s_sre_telegram_tools",
            "tests.test_k8s_storage_draft",
            "tests.test_k8s_transition_runner_artifacts",
            "tests.test_k8s_transition_runner_install_tools",
            "tests.test_k8s_transition_runner_policy",
        )
        actual_modules = tuple(item for item in suite.command if item.startswith("tests.test_k8s_"))
        self.assertEqual(actual_modules, expected_modules)


if __name__ == "__main__":
    unittest.main()
