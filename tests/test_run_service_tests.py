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


if __name__ == "__main__":
    unittest.main()
