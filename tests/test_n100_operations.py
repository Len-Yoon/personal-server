import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "run-n100-operations.sh"
ALLOWED = {
    "diagnose",
    "deploy_safe_crawler",
    "verify_news_observability",
    "apply_news_observability",
}


class N100OperationsTests(unittest.TestCase):
    def run_operation(self, operation, *extra, env=None):
        merged = os.environ.copy()
        merged.update(
            {
                "N100_OPERATIONS_PROJECT_ROOT": str(ROOT),
                "N100_OPERATIONS_ACTIONS_ROOT": str(ROOT),
                "N100_OPERATIONS_DEPLOY_SHA": "a" * 40,
            }
        )
        if env:
            merged.update(env)
        return subprocess.run(
            ["bash", str(SCRIPT), operation, *extra],
            cwd=ROOT,
            env=merged,
            text=True,
            capture_output=True,
        )

    def test_unknown_operation_is_rejected(self):
        result = self.run_operation("unknown")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("n100_operation=invalid status=FAIL", result.stdout + result.stderr)
        self.assertNotIn("kubectl", result.stdout + result.stderr)

    def test_extra_arguments_are_rejected(self):
        result = self.run_operation("diagnose", "extra")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("n100_operation=invalid status=FAIL", result.stdout + result.stderr)

    def test_allowlist_is_exactly_the_four_operations(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for operation in ALLOWED:
            self.assertIn(operation, text)

    def test_deploy_requires_a_valid_sha_and_fixed_service(self):
        result = self.run_operation(
            "deploy_safe_crawler",
            env={"N100_OPERATIONS_DEPLOY_SHA": "not-a-sha"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("n100_operation=deploy_safe_crawler status=FAIL", result.stdout + result.stderr)
        self.assertNotIn("super-secret", result.stdout + result.stderr)

    def test_apply_uses_only_two_fixed_manifests_without_secret_values(self):
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory) / "bin"
            bin_dir.mkdir()
            calls = Path(directory) / "calls"
            fake_sudo = bin_dir / "sudo"
            fake_sudo.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$N100_TEST_CALLS\"\n"
                "if [[ \"$*\" == *'apply --dry-run=client'* ]]; then exit 0; fi\n"
                "if [[ \"$*\" == *'apply -f'* ]]; then exit 0; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_sudo.chmod(0o755)
            env = {
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "N100_TEST_CALLS": str(calls),
            }
            result = self.run_operation("apply_news_observability", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = result.stdout + result.stderr
            self.assertIn("n100_operation=apply_news_observability status=PASS", output)
            self.assertNotIn("super-secret", output)
            entries = calls.read_text(encoding="utf-8").splitlines()
            apply_entries = [entry for entry in entries if "apply" in entry]
            self.assertEqual(len(apply_entries), 4)
            self.assertTrue(all("-n k3s kubectl" in entry for entry in apply_entries))
            self.assertTrue(any("crawler-news-observability.yaml" in entry for entry in apply_entries))
            self.assertTrue(any("prometheus-rule.yaml" in entry for entry in apply_entries))
            self.assertFalse(any("secret" in entry.lower() and "crawler-news-metrics" not in entry for entry in apply_entries))

    def test_script_is_not_an_arbitrary_command_runner(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('eval "', text)
        self.assertIn("sudo -n k3s kubectl", text)


if __name__ == "__main__":
    unittest.main()
