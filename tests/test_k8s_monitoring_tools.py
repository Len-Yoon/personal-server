import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/k8s/tools/monitoring-preflight.sh"


class MonitoringPreflightBehaviorTest(unittest.TestCase):
    def run_tool(
        self,
        *,
        kubectl_nodes="n100 Ready",
        storageclass=True,
        helm3=True,
        disk_available=9 * 1024 * 1024,
    ):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            calls = directory_path / "calls"
            sudo = directory_path / "sudo"
            helm = directory_path / "helm"
            df = directory_path / "df"
            sudo.write_text(
                "#!/bin/sh\n"
                "printf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  'k3s kubectl get nodes --no-headers')\n"
                f"    printf '%s\\n' '{kubectl_nodes}'\n"
                "    exit 0 ;;\n"
                "  'k3s kubectl get storageclass local-path')\n"
                + ("    printf 'local-path\\n'; exit 0 ;;\n" if storageclass else "    exit 1 ;;\n")
                + "  *) exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            helm.write_text(
                "#!/bin/sh\n"
                "printf 'helm %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$1\" in\n"
                "  version) "
                + ("printf 'v3.16.4+g\\n'; exit 0 ;;\n" if helm3 else "printf 'v2.17.0\\n'; exit 0 ;;\n")
                + "  show) printf 'version: 88.6.1\\n'; exit 0 ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            df.write_text(
                "#!/bin/sh\n"
                "printf 'df %s\\n' \"$*\" >> \"$CALLS\"\n"
                "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'\n"
                f"printf '/dev/sda 100000 1000 {disk_available} 2%% /var/lib/rancher/k3s/storage\\n'\n",
                encoding="utf-8",
            )
            for tool in (sudo, helm, df):
                tool.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{directory}:{os.environ['PATH']}",
                "CALLS": str(calls),
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

    def test_fails_closed_when_k3s_node_is_not_ready(self):
        result, _ = self.run_tool(kubectl_nodes="n100 NotReady")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_passes_when_a_node_is_cordoned_but_ready(self):
        result, _ = self.run_tool(kubectl_nodes="n100 Ready,SchedulingDisabled")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=PASS"))

    def test_fails_closed_when_any_node_is_not_ready(self):
        result, _ = self.run_tool(kubectl_nodes="n100 Ready\nn101 NotReady")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_fails_closed_when_local_path_storageclass_is_missing(self):
        result, _ = self.run_tool(storageclass=False)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_fails_closed_when_helm_is_not_version_three(self):
        result, _ = self.run_tool(helm3=False)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_fails_closed_when_storage_has_less_than_eight_gib_available(self):
        result, _ = self.run_tool(disk_available=(8 * 1024 * 1024) - 1)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_fails_closed_when_storage_has_no_space_available(self):
        result, _ = self.run_tool(disk_available=0)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_passes_and_reports_chart_version_when_all_checks_succeed(self):
        result, calls = self.run_tool()
        self.assertEqual(result.returncode, 0)
        self.assertIn("chart_version=88.6.1", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=PASS"))
        self.assertIn("k3s kubectl get nodes --no-headers", calls)
        self.assertIn("k3s kubectl get storageclass local-path", calls)
        self.assertIn("helm show chart prometheus-community/kube-prometheus-stack --version 88.6.1", calls)
        self.assertIn("df -Pk /var/lib/rancher/k3s/storage", calls)

    def test_only_read_only_commands_are_invoked(self):
        result, calls = self.run_tool()
        self.assertEqual(result.returncode, 0)
        for forbidden in (" repo ", " install", " upgrade", " uninstall", " create", " apply", " delete", "secret"):
            self.assertNotIn(forbidden, calls)
        self.assertNotIn("http://", calls)


if __name__ == "__main__":
    unittest.main()
