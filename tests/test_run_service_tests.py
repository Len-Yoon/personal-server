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


if __name__ == "__main__":
    unittest.main()
