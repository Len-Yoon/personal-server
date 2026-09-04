import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "infra/k8s/tools/install-transition-runner.sh"
PREFLIGHT = ROOT / "infra/k8s/tools/transition-runner-preflight.sh"


class TransitionRunnerInstallToolsTests(unittest.TestCase):
    def run_tool(self, tool, *args, env=None):
        return subprocess.run(
            ["bash", str(tool), *args], cwd=ROOT, text=True,
            capture_output=True, env=env or os.environ.copy(),
        )

    def test_installer_refuses_apply_without_explicit_flag(self):
        result = self.run_tool(INSTALLER)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("transition_runner_install=FAIL", result.stdout + result.stderr)

    def test_preflight_never_prints_credential_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            credential_dir = Path(directory)
            secret = "rclone-config-passphrase=do-not-print\n"
            for name in ("rclone-config", "rclone-config-passphrase", "age-identity"):
                path = credential_dir / f"{name}.cred"
                path.write_text(secret, encoding="utf-8")
                path.chmod(0o600)
            environment = os.environ.copy()
            environment["TRANSITION_PREFLIGHT_CREDENTIAL_DIR"] = str(credential_dir)
            result = self.run_tool(PREFLIGHT, env=environment)
        self.assertNotIn(secret.strip(), result.stdout)
        self.assertNotIn("do-not-print", result.stdout)

    def test_preflight_is_read_only_by_default(self):
        result = self.run_tool(PREFLIGHT)
        self.assertNotIn("mkdir", result.stdout)
        self.assertNotIn("install", result.stdout)


if __name__ == "__main__":
    unittest.main()
