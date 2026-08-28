import re
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/k8s/tools/portal-secret-shadow-smoke.sh"
DOC = ROOT / "docs/k3s-flux-transition-draft.md"


class PortalSecretShadowSmokeContractTest(unittest.TestCase):
    def test_script_exists_and_is_bash(self):
        self.assertTrue(SCRIPT.is_file())
        self.assertTrue(SCRIPT.read_text().startswith("#!/usr/bin/env bash"))

    def test_secret_and_cluster_safety_contract(self):
        text = SCRIPT.read_text()
        for required in (
            'k3s secrets-encrypt status',
            'Encryption Status: Enabled',
            'DELETE_PASSWORD',
            'FILE_MANAGER_PASSWORD',
            'ADMIN_STATUS_PASSWORD',
            'FILE_MANAGER_ACCESS_PASSWORD',
            'automountServiceAccountToken: false',
            'imagePullPolicy: Never',
            'FILE_STORAGE_PATH',
            'emptyDir',
            'immutable: true',
            'rm -f -- "$TMP_SECRET"; TMP_SECRET=""',
            'portal_secret_shadow=PASS',
            'portal_secret_shadow=FAIL',
            'portal_secret_shadow_run_id=',
            'kind: NetworkPolicy',
            'policyTypes:',
        ):
            self.assertIn(required, text)

        self.assertNotRegex(text, r'(^|\n)\s*(source|\.)\s+[^\n]*\.env')
        for forbidden in ("kind: Service", "kind: Ingress", "NodePort", "hostPath", "persistentVolumeClaim", "caddy", "compose", "flux", "scheduler"):
            self.assertNotIn(forbidden.lower(), text.lower())
        self.assertNotRegex(text, r"trap[^\n]*EXIT")
        self.assertIn("trap", text.lower())

    def test_exact_core_key_allowlist_and_cleanup_assertions(self):
        text = SCRIPT.read_text()
        keys = re.findall(r"(?:DELETE_PASSWORD|FILE_MANAGER_PASSWORD|ADMIN_STATUS_PASSWORD|FILE_MANAGER_ACCESS_PASSWORD)", text)
        self.assertTrue(keys)
        self.assertNotIn("HOMEOPS_EXECUTOR_SHARED_SECRET", text)
        self.assertNotIn("PORTFOLIO_ADMIN_PASSWORD", text)
        for required in (
            'cleanup',
            'delete namespace',
            'assert_namespace_absent',
            'assert_image_absent',
            'mktemp',
            'chmod 600',
            'docker tag personal-server-portal-web:latest',
            'docker save',
            'k3s ctr',
            '--cleanup',
            'timeout',
            '--wait=true --timeout=120s',
        ):
            self.assertIn(required, text)

    def test_encryption_gate_makes_no_resource_attempt(self):
        secret_value = "must-not-appear"
        with tempfile.TemporaryDirectory() as directory:
            fake_sudo = Path(directory) / "sudo"
            calls = Path(directory) / "calls"
            fake_sudo.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> {calls}\n"
                "if [ \"$1\" = k3s ] && [ \"$2\" = secrets-encrypt ] && [ \"$3\" = status ]; then\n"
                "  printf '%s\\n' 'Encryption Status: Disabled'\n"
                "  exit 0\n"
                "fi\n"
                "exit 99\n"
            )
            fake_sudo.chmod(0o755)
            env_file = Path(directory) / ".env"
            env_file.write_text("\n".join(f"{key}={secret_value}" for key in (
                "DELETE_PASSWORD", "FILE_MANAGER_PASSWORD", "ADMIN_STATUS_PASSWORD", "FILE_MANAGER_ACCESS_PASSWORD"
            )))
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                env={**os.environ, "PATH": f"{directory}:{os.environ['PATH']}", "PORTAL_ENV_FILE": str(env_file), "RUN_ID": "gate"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(secret_value, result.stdout + result.stderr)
            self.assertEqual(calls.read_text().splitlines(), ["k3s secrets-encrypt status"])

    def test_docs_define_isolated_manual_scope(self):
        text = DOC.read_text()
        for required in (
            "isolated manual smoke",
            "optional HomeOps/portfolio",
            "data copy",
            "Caddy routing",
            "actual cutover",
        ):
            self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main()
