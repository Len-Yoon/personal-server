import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "infra/k8s/tools"
SCRIPT = ROOT / "infra/k8s/tools/monitoring-preflight.sh"


class MonitoringPreflightBehaviorTest(unittest.TestCase):
    def run_preflight(
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
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                env={**os.environ, "PATH": f"{directory}:{os.environ['PATH']}", "CALLS": str(calls)},
                capture_output=True,
                text=True,
                check=False,
            )
            recorded = calls.read_text(encoding="utf-8") if calls.exists() else ""
            return result, recorded

    def test_fails_closed_when_k3s_node_is_not_ready(self):
        result, _ = self.run_preflight(kubectl_nodes="n100 NotReady")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_passes_when_a_node_is_cordoned_but_ready(self):
        result, _ = self.run_preflight(kubectl_nodes="n100 Ready,SchedulingDisabled")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=PASS"))

    def test_fails_closed_when_any_node_is_not_ready(self):
        result, _ = self.run_preflight(kubectl_nodes="n100 Ready\nn101 NotReady")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_fails_closed_when_local_path_storageclass_is_missing(self):
        result, _ = self.run_preflight(storageclass=False)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_fails_closed_when_helm_is_not_version_three(self):
        result, _ = self.run_preflight(helm3=False)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_fails_closed_when_storage_has_less_than_eight_gib_available(self):
        result, _ = self.run_preflight(disk_available=(8 * 1024 * 1024) - 1)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_fails_closed_when_storage_has_no_space_available(self):
        result, _ = self.run_preflight(disk_available=0)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_passes_and_reports_chart_version_when_all_checks_succeed(self):
        result, calls = self.run_preflight()
        self.assertEqual(result.returncode, 0)
        self.assertIn("chart_version=88.6.1", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=PASS"))
        self.assertIn("k3s kubectl get nodes --no-headers", calls)
        self.assertIn("k3s kubectl get storageclass local-path", calls)
        self.assertIn("helm show chart prometheus-community/kube-prometheus-stack --version 88.6.1", calls)
        self.assertIn("df -Pk /var/lib/rancher/k3s/storage", calls)

    def test_only_read_only_commands_are_invoked(self):
        result, calls = self.run_preflight()
        self.assertEqual(result.returncode, 0)
        for forbidden in (" repo ", " install", " upgrade", " uninstall", " create", " apply", " delete", "secret"):
            self.assertNotIn(forbidden, calls)
        self.assertNotIn("http://", calls)


class MonitoringToolsTest(unittest.TestCase):
    def run_tool(self, name, *args, stubs=None, env_overrides=None):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            calls = directory_path / "calls"
            for command, body in (stubs or {}).items():
                path = directory_path / command
                path.write_text(body, encoding="utf-8")
                path.chmod(0o755)
            env = {**os.environ, "PATH": f"{directory}:{os.environ['PATH']}", "CALLS": str(calls)}
            env.update(env_overrides or {})
            result = subprocess.run(
                ["bash", str(TOOLS / name), *args],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            recorded = calls.read_text(encoding="utf-8") if calls.exists() else ""
            return result, recorded

    def test_install_requires_explicit_apply_or_render(self):
        result, _ = self.run_tool("monitoring-install.sh")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--apply", result.stderr)

    def test_uninstall_requires_explicit_uninstall(self):
        result, _ = self.run_tool("monitoring-uninstall.sh")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--uninstall", result.stderr)

    def test_apply_uses_pinned_helm_release_arguments(self):
        result, calls = self.run_tool(
            "monitoring-install.sh",
            "--apply",
            stubs={
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
                "preflight": "#!/bin/sh\nprintf 'preflight\\n' >> \"$CALLS\"\nprintf 'monitoring_preflight=PASS\\n'\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get namespace monitoring'*) printf 'Error from server (NotFound): namespaces \\\"monitoring\\\" not found\\n' >&2; exit 1;;\n"
                "  *'create namespace monitoring'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
            },
            env_overrides={"MONITORING_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(
            "helm upgrade --install personal-server-monitoring prometheus-community/kube-prometheus-stack "
            "--namespace monitoring --version 88.6.1 "
            "--values infra/k8s/monitoring/values.n100.yaml --wait --timeout 10m",
            calls,
        )
        self.assertEqual(result.stdout.rstrip().splitlines()[-1], "monitoring_install=PASS")

    def test_apply_refuses_to_upgrade_when_preflight_fails(self):
        result, calls = self.run_tool(
            "monitoring-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nprintf 'monitoring_preflight=FAIL\\n'\nexit 1\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"MONITORING_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("upgrade --install", calls)

    def test_apply_refuses_preexisting_monitoring_namespace(self):
        result, calls = self.run_tool(
            "monitoring-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nprintf 'monitoring_preflight=PASS\\n'\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"MONITORING_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("get namespace monitoring", calls)
        self.assertNotIn("upgrade --install", calls)

    def test_failed_apply_rolls_back_release_and_new_namespace(self):
        result, calls = self.run_tool(
            "monitoring-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nprintf 'monitoring_preflight=PASS\\n'\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get namespace monitoring'*) printf 'Error from server (NotFound): namespaces \\\"monitoring\\\" not found\\n' >&2; exit 1;;\n"
                "  *'create namespace monitoring'*) exit 0;;\n"
                "  *'delete namespace monitoring'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\ncase \"$1\" in upgrade) exit 9;; uninstall) exit 0;; esac\nexit 0\n",
            },
            env_overrides={"MONITORING_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("helm upgrade --install", calls)
        self.assertIn("helm uninstall personal-server-monitoring --namespace monitoring --wait --timeout 5m", calls)
        self.assertIn("delete namespace monitoring", calls)

    def test_apply_fails_closed_when_namespace_lookup_is_not_not_found(self):
        result, calls = self.run_tool(
            "monitoring-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nprintf 'monitoring_preflight=PASS\\n'\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\nprintf 'forbidden\\n' >&2\nexit 1\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"MONITORING_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("upgrade --install", calls)
        self.assertNotIn("delete namespace", calls)

    def test_apply_fails_closed_on_namespace_create_race_without_deletion(self):
        result, calls = self.run_tool(
            "monitoring-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nprintf 'monitoring_preflight=PASS\\n'\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get namespace monitoring'*) printf 'Error from server (NotFound): namespaces \\\"monitoring\\\" not found\\n' >&2; exit 1;;\n"
                "  *'create namespace monitoring'*) printf 'AlreadyExists\\n' >&2; exit 1;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"MONITORING_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("upgrade --install", calls)
        self.assertNotIn("delete namespace", calls)

    def test_render_only_calls_helm_template(self):
        result, calls = self.run_tool(
            "monitoring-install.sh",
            "--render",
            stubs={
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(
            "helm template personal-server-monitoring prometheus-community/kube-prometheus-stack "
            "--namespace monitoring --version 88.6.1 --values infra/k8s/monitoring/values.n100.yaml",
            calls,
        )
        self.assertNotIn("upgrade", calls)

    def test_verify_fails_when_grafana_service_is_not_cluster_ip(self):
        result, _ = self.run_tool(
            "monitoring-verify.sh",
            stubs={
                "sudo": "#!/bin/sh\ncase \"$*\" in\n"
                "  *'get pvc'*) printf 'prometheus Bound\\ngrafana Bound\\n'; exit 0;;\n"
                "  *'get pods'*) exit 0;;\n"
                "  *'wait --for=condition=Ready pod --all'*) exit 0;;\n"
                "  *'get service personal-server-monitoring-grafana'*) printf 'NodePort\\n'; exit 0;;\n"
                "  *'get configmap'*) printf 'configmap/grafana-dashboard\\n'; exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.rstrip().endswith("monitoring_verify=FAIL"))

    def test_uninstall_preserves_data_without_delete_data(self):
        result, calls = self.run_tool(
            "monitoring-uninstall.sh",
            "--uninstall",
            stubs={
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("helm uninstall personal-server-monitoring --namespace monitoring --wait --timeout 5m", calls)
        self.assertNotIn("delete pvc", calls)
        self.assertNotIn("delete namespace", calls)

    def test_delete_data_requires_explicit_flag_and_deletes_pvc_and_namespace(self):
        result, calls = self.run_tool(
            "monitoring-uninstall.sh",
            "--uninstall",
            "--delete-data",
            stubs={
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("delete pvc --all", calls)
        self.assertIn("delete namespace monitoring", calls)

    def test_verify_port_forward_binds_localhost_and_does_not_read_secrets(self):
        result, calls = self.run_tool(
            "monitoring-verify.sh",
            "--port-forward-check",
            stubs={
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get pvc'*) printf 'prometheus Bound\\ngrafana Bound\\n'; exit 0;; *'get pods'*) exit 0;;\n"
                "  *'wait --for=condition=Ready pod --all'*) exit 0;;\n"
                "  *'get service personal-server-monitoring-grafana'*) printf 'ClusterIP\\n'; exit 0;;\n"
                "  *'get configmap'*) printf 'configmap/grafana-dashboard\\n'; exit 0;;\n"
                "  *'port-forward'*) sleep 30;;\n  *) exit 1;;\n"
                "esac\n",
                "curl": "#!/bin/sh\nprintf 'curl %s\\n' \"$*\" >> \"$CALLS\"\nprintf '{\"status\":\"success\",\"data\":{\"active\":[{\"health\":\"up\"}]}}\\n'\nexit 0\n",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("port-forward --address 127.0.0.1 service/personal-server-monitoring-grafana 3000:80", calls)
        self.assertIn("http://127.0.0.1:3000/login", calls)
        self.assertNotIn("get secret", calls)
        self.assertNotIn("jsonpath='{.data", calls)

    def test_verify_fails_when_port_forward_login_check_fails(self):
        result, _ = self.run_tool(
            "monitoring-verify.sh",
            "--port-forward-check",
            stubs={
                "sudo": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'get pvc'*) printf 'prometheus Bound\\ngrafana Bound\\n'; exit 0;; *'get pods'*) exit 0;;\n"
                "  *'wait --for=condition=Ready pod --all'*) exit 0;;\n"
                "  *'get service personal-server-monitoring-grafana'*) printf 'ClusterIP\\n'; exit 0;;\n"
                "  *'get configmap'*) printf 'configmap/grafana-dashboard\\n'; exit 0;;\n"
                "  *'port-forward'*) sleep 30;;\n  *) exit 1;;\n"
                "esac\n",
                "curl": "#!/bin/sh\nexit 22\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.rstrip().endswith("monitoring_verify=FAIL"))

    def test_verify_fails_when_port_forward_exits_before_login_check(self):
        result, _ = self.run_tool(
            "monitoring-verify.sh",
            "--port-forward-check",
            stubs={
                "sudo": "#!/bin/sh\ncase \"$*\" in\n"
                "  *'get pvc'*) printf 'prometheus Bound\\ngrafana Bound\\n'; exit 0;; *'get pods'*) exit 0;;\n"
                "  *'wait --for=condition=Ready pod --all'*) exit 0;;\n"
                "  *'get service personal-server-monitoring-grafana'*) printf 'ClusterIP\\n'; exit 0;;\n"
                "  *'get configmap'*) printf 'configmap/grafana-dashboard\\n'; exit 0;;\n"
                "  *'port-forward'*) exit 1;; *) exit 1;;\n"
                "esac\n",
                "curl": "#!/bin/sh\nexit 0\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.rstrip().endswith("monitoring_verify=FAIL"))


if __name__ == "__main__":
    unittest.main()
