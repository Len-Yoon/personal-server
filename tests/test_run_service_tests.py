import subprocess
import sys
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
