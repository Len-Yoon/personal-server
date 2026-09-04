import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "infra/k8s/tools/validate-transition-runner-policy.py"
POLICY = ROOT / "infra/k8s/transition-runner/policy/runner-policy.json"


class TransitionRunnerPolicyTest(unittest.TestCase):
    def run_policy(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return subprocess.run(["python3", str(VALIDATOR), str(path)], capture_output=True, text=True)

    def test_repository_policy_is_valid(self):
        result = subprocess.run(["python3", str(VALIDATOR), str(POLICY)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_policy_rejects_mutable_image_tag(self):
        data = json.loads(POLICY.read_text(encoding="utf-8"))
        data["services"][0]["image"] = "personal-server-book-memo:latest"
        self.assertNotEqual(self.run_policy(data).returncode, 0)

    def test_policy_rejects_unknown_key(self):
        data = json.loads(POLICY.read_text(encoding="utf-8"))
        data["unexpected"] = True
        self.assertNotEqual(self.run_policy(data).returncode, 0)

    def test_policy_rejects_path_traversal(self):
        data = json.loads(POLICY.read_text(encoding="utf-8"))
        data["services"][0]["pvc"] = "../host"
        self.assertNotEqual(self.run_policy(data).returncode, 0)

    def test_policy_rejects_attacker_pvc_even_when_name_is_safe(self):
        data = json.loads(POLICY.read_text(encoding="utf-8"))
        data["services"][0]["pvc"] = "crawler-worker-data-evil"
        self.assertNotEqual(self.run_policy(data).returncode, 0)

    def test_policy_rejects_unapproved_image_registry(self):
        data = json.loads(POLICY.read_text(encoding="utf-8"))
        digest = "d" * 64
        data["services"][0]["image"] = f"evil.example/crawler-worker@sha256:{digest}"
        self.assertNotEqual(self.run_policy(data).returncode, 0)

    def test_policy_rejects_nonpositive_timeout(self):
        data = json.loads(POLICY.read_text(encoding="utf-8"))
        data["timeouts"]["backup"] = 0
        self.assertNotEqual(self.run_policy(data).returncode, 0)
