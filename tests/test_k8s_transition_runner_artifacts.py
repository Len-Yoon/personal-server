import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "infra/k8s/transition-runner/runner/personal-server-transition-runner"
UNIT = ROOT / "infra/k8s/transition-runner/systemd/personal-server-transition.service"


class TransitionRunnerArtifactTest(unittest.TestCase):
    def test_runner_rejects_arguments(self):
        result = subprocess.run([str(RUNNER), "book-memo"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def test_runner_never_references_user_writable_runtime_paths(self):
        text = RUNNER.read_text(encoding="utf-8")
        self.assertNotIn("/mnt/c", text)
        self.assertNotIn(".config/rclone", text)
        self.assertNotIn("/Users/", text)
        self.assertNotIn("docker compose", text)

    def test_runner_has_fixed_lifecycle_and_lock_root(self):
        text = RUNNER.read_text(encoding="utf-8")
        for phase in ("preflight", "backup", "stop-compose", "copy-pvc", "start-k3s", "verify-private", "record"):
            self.assertIn(phase, text)
        self.assertIn("/var/lib/personal-server-transition/locks", text)

    def test_systemd_unit_is_root_and_sandboxed(self):
        text = UNIT.read_text(encoding="utf-8")
        self.assertIn("User=root", text)
        self.assertIn("LoadCredentialEncrypted=", text)
        self.assertIn("PrivateTmp=yes", text)
        self.assertIn("ProtectHome=yes", text)
        self.assertIn("ProtectSystem=strict", text)
        self.assertIn("InaccessiblePaths=/mnt/c", text)
        self.assertIn("ReadWritePaths=/var/lib/personal-server-transition", text)
        self.assertIn("ExecStart=/usr/local/libexec/personal-server-transition/personal-server-transition-runner", text)

    def test_shell_runner_has_valid_syntax(self):
        self.assertEqual(subprocess.run(["bash", "-n", str(RUNNER)]).returncode, 0)
