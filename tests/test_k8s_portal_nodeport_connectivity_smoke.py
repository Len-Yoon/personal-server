import re
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/k8s/tools/portal-nodeport-connectivity-smoke.sh"


class PortalNodePortConnectivitySmokeContractTest(unittest.TestCase):
    def test_script_exists_and_is_bash(self):
        self.assertTrue(SCRIPT.is_file())
        self.assertTrue(SCRIPT.read_text().startswith("#!/usr/bin/env bash"))

    def test_uses_isolated_resources_and_fixed_nodeport(self):
        text = SCRIPT.read_text()
        for required in (
            "RUN_ID",
            "portal-nodeport-smoke-",
            "kind: Namespace",
            "kind: Deployment",
            "kind: Service",
            "type: NodePort",
            "nodePort: 32081",
            "portal-nodeport-smoke-deployment",
            "portal-nodeport-smoke-service",
            "image: busybox:1.36",
            "/health",
        ):
            self.assertIn(required, text)
        self.assertRegex(text, r"valid_run_id")
        self.assertRegex(text, r'NS="portal-nodeport-smoke-\$\{run_id_lc\}"')

    def test_probes_from_caddy_container_without_public_route_change(self):
        text = SCRIPT.read_text()
        self.assertIn('CADDY_CONTAINER="${CADDY_CONTAINER:-personal-server-caddy-1}"', text)
        self.assertIn("docker exec", text)
        self.assertIn("host.docker.internal", text)
        self.assertRegex(text, r"docker exec[^\n]+curl")
        self.assertNotIn("Caddyfile", text)
        self.assertNotIn("docker compose", text.lower())
        self.assertNotIn("portal-web:8000", text)

    def test_cleanup_is_exact_and_runs_on_failure(self):
        text = SCRIPT.read_text()
        for required in (
            "cleanup()",
            "trap on_signal INT TERM HUP",
            "delete namespace",
            "--ignore-not-found",
            "--wait=true",
            "assert_namespace_absent",
            "portal_nodeport_connectivity=PASS",
            "portal_nodeport_connectivity=FAIL",
        ):
            self.assertIn(required, text)
        self.assertNotRegex(text, r"trap[^\n]+EXIT")
        self.assertNotRegex(text, r"kubectl delete namespace [^\n]+--grace-period=0 --force")

    def test_run_id_rejects_shell_metacharacters(self):
        text = SCRIPT.read_text()
        self.assertRegex(text, r"\*\[!a-zA-Z0-9-")
        self.assertIn("usage: [--cleanup RUN_ID]", text)

    def test_apply_failure_cleans_up_partial_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            calls = directory_path / "calls"
            fake_sudo = directory_path / "sudo"
            fake_sudo.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> '{calls}'\n"
                "if [ \"$1\" = k3s ] && [ \"$2\" = kubectl ] && [ \"$3\" = get ]; then exit 1; fi\n"
                "if [ \"$1\" = k3s ] && [ \"$2\" = kubectl ] && [ \"$3\" = apply ]; then exit 42; fi\n"
                "if [ \"$1\" = k3s ] && [ \"$2\" = kubectl ] && [ \"$3\" = delete ]; then exit 0; fi\n"
                "exit 1\n"
            )
            fake_sudo.chmod(0o755)
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                env={**os.environ, "PATH": f"{directory}:{os.environ['PATH']}", "RUN_ID": "apply-failure"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            recorded_calls = calls.read_text().splitlines()
            self.assertTrue(any("k3s kubectl apply" in call for call in recorded_calls))
            self.assertTrue(any("k3s kubectl delete namespace portal-nodeport-smoke-apply-failure" in call for call in recorded_calls))


if __name__ == "__main__":
    unittest.main()
