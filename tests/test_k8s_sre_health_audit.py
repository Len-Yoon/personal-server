import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/k8s/tools/sre-health-audit.sh"


class SreHealthAuditBehaviorTest(unittest.TestCase):
    def run_audit(self, *, nodes="n100 Ready", services=None, ps=None, extra_env=None):
        services = services or ["portal-web", "crawler-worker"]
        ps = ps or ["portal-web|running|healthy", "crawler-worker|running|"]
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            calls = directory_path / "calls"
            sudo = directory_path / "sudo"
            docker = directory_path / "docker"
            sudo.write_text(
                "#!/bin/sh\n"
                "printf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "if [ \"$1 $2 $3 $4\" = 'k3s kubectl get nodes' ]; then\n"
                f"  printf '%s\\n' '{nodes}'\n"
                "  exit 0\n"
                "fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            docker.write_text(
                "#!/bin/sh\n"
                "printf 'docker %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *' config --services')\n"
                + "    printf '%s\\n' " + " ".join(f"'{service}'" for service in services) + "\n"
                + "    ;;\n"
                "  *' ps --all'*)\n"
                + "    printf '%s\\n' " + " ".join(f"'{row}'" for row in ps) + "\n"
                + "    ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            sudo.chmod(0o755)
            docker.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{directory}:{os.environ['PATH']}",
                "CALLS": str(calls),
                **(extra_env or {}),
            }
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            recorded = calls.read_text(encoding="utf-8") if calls.exists() else ""
            return result, recorded

    def test_passes_when_a_node_is_ready_and_all_compose_services_are_running(self):
        result, _ = self.run_audit()
        self.assertEqual(result.returncode, 0)
        self.assertIn("k3s_nodes=PASS", result.stdout)
        self.assertIn("compose_containers=PASS", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("sre_health=PASS"))

    def test_fails_when_no_k3s_node_is_ready(self):
        result, _ = self.run_audit(nodes="n100 NotReady")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("k3s_nodes=FAIL", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("sre_health=FAIL"))

    def test_passes_when_ready_node_is_cordoned(self):
        result, _ = self.run_audit(nodes="n100 Ready,SchedulingDisabled")
        self.assertEqual(result.returncode, 0)
        self.assertIn("k3s_nodes=PASS", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("sre_health=PASS"))

    def test_fails_when_a_service_is_stopped_or_unhealthy(self):
        result, _ = self.run_audit(
            ps=["portal-web|exited|", "crawler-worker|running|unhealthy"]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compose_containers=FAIL", result.stdout)
        self.assertIn("portal-web", result.stdout)
        self.assertIn("crawler-worker", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("sre_health=FAIL"))

    def test_help_and_unknown_argument_do_not_query_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory) / "calls"
            env = {**os.environ, "PATH": f"{directory}:{os.environ['PATH']}", "CALLS": str(calls)}
            help_result = subprocess.run(
                ["bash", str(SCRIPT), "--help"], env=env, capture_output=True, text=True
            )
            unknown_result = subprocess.run(
                ["bash", str(SCRIPT), "--bogus"], env=env, capture_output=True, text=True
            )
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("usage:", help_result.stdout.lower())
        self.assertNotEqual(unknown_result.returncode, 0)
        self.assertIn("unknown argument", unknown_result.stderr.lower())

    def test_help_rejects_trailing_arguments(self):
        result = subprocess.run(
            ["bash", str(SCRIPT), "--help", "extra"],
            env=os.environ,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown argument", result.stderr.lower())

    def test_only_read_only_commands_are_invoked(self):
        result, calls = self.run_audit()
        self.assertEqual(result.returncode, 0)
        for forbidden in (" start", " stop", " up", " down", " restart", " apply", " delete", " secret"):
            self.assertNotIn(forbidden, calls)
        self.assertNotIn("http://", calls)


if __name__ == "__main__":
    unittest.main()
