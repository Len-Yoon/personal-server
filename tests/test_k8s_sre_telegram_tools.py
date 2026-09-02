import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "infra" / "k8s" / "tools"


def read_tool(name: str) -> str:
    path = TOOLS / name
    if not path.is_file():
        raise AssertionError(f"required tool is missing: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


class SreTelegramToolContractTest(unittest.TestCase):
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

    def test_preflight_invokes_only_read_only_k3s_and_helm_commands(self):
        script = read_tool("sre-telegram-preflight.sh")

        self.assertIn("kubectl get nodes --no-headers", script)
        self.assertIn('helm status "$RELEASE" --namespace "$NAMESPACE"', script)
        self.assertNotRegex(script, r"kubectl (apply|create|delete|patch|replace|edit)")
        self.assertNotRegex(script, r"helm (install|upgrade|uninstall|rollback)")

    def test_render_never_imports_image_or_applies_resources(self):
        script = read_tool("sre-telegram-install.sh")
        render = script.split("render()", 1)[1].split("require_secret_contract", 1)[0]

        self.assertIn("helm template", render)
        self.assertIn("--dry-run=client", render)
        self.assertNotIn("ctr -n k8s.io images import", render)
        self.assertNotIn("docker build", render)
        self.assertNotIn("--apply", render)

    def test_apply_requires_all_secret_keys_by_name_without_reading_values(self):
        script = read_tool("sre-telegram-install.sh")

        for key in ("telegram_bot_token", "allowed_chat_id", "alertmanager_auth_token", "alertmanager.yaml"):
            self.assertIn(key, script)
        self.assertRegex(script, r"kubectl(?: -n [^ ]+)? describe secret")
        self.assertNotRegex(script, r"secret[^\n]*(jsonpath|\.data|-o +yaml|-o +json)")
        self.assertIn('mode="${1:---render}"', script)
        self.assertIn("--apply)", script)

    def test_verify_never_reads_or_prints_secret_data(self):
        script = read_tool("sre-telegram-verify.sh")

        self.assertIn("port-forward --address 127.0.0.1", script)
        self.assertIn("/healthz", script)
        self.assertIn("/api/v1/targets", script)
        self.assertNotRegex(script, r"kubectl(?:\s+-\S+\s+\S+)*\s+get\s+secrets?")
        self.assertNotRegex(script, r"kubectl(?:\s+-\S+\s+\S+)*\s+describe\s+secrets?")
        self.assertNotIn(".data", script)

    def test_secret_guidance_only_names_required_keys_and_manual_procedure(self):
        script = read_tool("sre-telegram-secret-template.sh")

        self.assertIn("N100", script)
        self.assertIn("manual", script.lower())
        self.assertIn("telegram_bot_token", script)
        self.assertIn("allowed_chat_id", script)
        self.assertIn("alertmanager_auth_token", script)
        self.assertIn("alertmanager.yaml", script)
        self.assertNotRegex(script, r"kubectl +(create|apply|patch|replace)")
        self.assertNotRegex(script, r"(echo|printf).*TOKEN")

    def test_preflight_rejects_a_non_deployed_release_even_when_helm_status_returns_zero(self):
        result, _ = self.run_tool(
            "sre-telegram-preflight.sh",
            stubs={
                "sudo": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'get nodes --no-headers'*) printf 'n100 Ready\\n'; exit 0;;\n"
                "  *'get deployment personal-server-monitoring-grafana'*) printf '1\\n'; exit 0;;\n"
                "  *'get statefulset -l'*) printf '1\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-relay-runtime'*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-alertmanager-config'*) printf 'alertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'ctr version'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  status) printf '{\"info\":{\"status\":\"failed\"}}\\n'; exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "docker": "#!/bin/sh\nexit 0\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_preflight=FAIL"))

    def test_preflight_rejects_zero_byte_secret_keys_without_reading_values(self):
        result, _ = self.run_tool(
            "sre-telegram-preflight.sh",
            stubs={
                "sudo": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'get nodes --no-headers'*) printf 'n100 Ready\\n'; exit 0;;\n"
                "  *'get deployment personal-server-monitoring-grafana'*) printf '1\\n'; exit 0;;\n"
                "  *'get statefulset -l'*) printf '1\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-relay-runtime'*) printf 'telegram_bot_token: 0 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-alertmanager-config'*) printf 'alertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'ctr version'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  status) printf '{\"info\":{\"status\":\"deployed\"}}\\n'; exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "docker": "#!/bin/sh\nexit 0\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("telegram_bot_token=", result.stdout)

    def test_preflight_uses_existing_prometheus_service_and_label_discovery(self):
        result, calls = self.run_tool(
            "sre-telegram-preflight.sh",
            stubs={
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get nodes --no-headers'*) printf 'n100 Ready\\n'; exit 0;;\n"
                "  *'get deployment personal-server-monitoring-grafana'*) printf '1\\n'; exit 0;;\n"
                "  *'get statefulset -l'*) printf '1\\n'; exit 0;;\n"
                "  *'get service personal-server-monitoring-prometheus'*) exit 0;;\n"
                "  *'describe secret sre-telegram-relay-runtime'*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-alertmanager-config'*) printf 'alertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'ctr version'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  status) printf '{\"info\":{\"status\":\"deployed\"}}\\n'; exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "docker": "#!/bin/sh\nexit 0\n",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_preflight=PASS"))
        self.assertIn("get statefulset -l app.kubernetes.io/name=prometheus", calls)
        self.assertIn("get service personal-server-monitoring-prometheus", calls)

    def test_install_stops_before_helm_when_image_save_fails(self):
        result, calls = self.run_tool(
            "sre-telegram-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nprintf 'sre_telegram_preflight=PASS\\n'\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'describe secret sre-telegram-relay-runtime'*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-alertmanager-config'*) printf 'alertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'apply --dry-run=client'*) exit 0;;\n"
                "  *images*import*) exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
                "docker": "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  build) exit 0;;\n"
                "  save) exit 7;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"SRE_TELEGRAM_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_install=FAIL"))
        self.assertNotIn("helm upgrade", calls)

    def test_install_stops_before_resource_create_when_imported_image_has_no_digest(self):
        result, calls = self.run_tool(
            "sre-telegram-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *describe*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\nalertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'apply --dry-run=client'*) exit 0;;\n"
                "  *'images list'*) printf 'REF TYPE DIGEST SIZE PLATFORMS LABELS\\npersonal-server-sre-telegram-relay:latest x <none> 1MB linux/amd64 -\\n'; exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
                "docker": "#!/bin/sh\n"
                "case \"$1\" in build) exit 0;; save) printf image-stream; exit 0;; esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"SRE_TELEGRAM_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_install=FAIL"))
        self.assertNotIn("create -f", calls)
        self.assertNotIn("helm upgrade", calls)

    def test_install_applies_relay_before_atomic_helm_upgrade_and_rolls_back_namespaced_resources(self):
        result, calls = self.run_tool(
            "sre-telegram-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nprintf 'sre_telegram_preflight=PASS\\n'\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *describe*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\nalertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'apply --dry-run=client'*) exit 0;;\n"
                "  *'RoleBinding-sre-telegram-relay-workload-reader.yaml'*) printf 'rolebinding.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader created\\n'; exit 0;;\n"
                "  *delete*) exit 0;;\n"
                "  *'images list'*) printf 'REF TYPE DIGEST SIZE PLATFORMS LABELS\\npersonal-server-sre-telegram-relay:latest x sha256:abc 1MB linux/amd64 -\\n'; exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
                "docker": "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  build) exit 0;;\n"
                "  save) printf 'image-stream'; exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$1\" in\n"
                "  template) exit 0;;\n"
                "  status) printf '{\"info\":{\"status\":\"deployed\",\"revision\":\"2\"}}\\n'; exit 0;;\n"
                "  upgrade) printf 'upgrade failed\\n' >&2; exit 1;;\n"
                "  *) exit 0;;\n"
                "esac\n",
            },
            env_overrides={"SRE_TELEGRAM_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_install=FAIL"))
        self.assertIn("monitoring-ConfigMap-sre-telegram-relay-state.yaml", calls)
        self.assertIn("helm upgrade", calls)
        base_index = calls.index("monitoring-ConfigMap-sre-telegram-relay-state.yaml")
        upgrade_index = calls.index("helm upgrade")
        self.assertLess(base_index, upgrade_index)
        self.assertIn("--atomic", calls)
        self.assertIn("-n personal-server delete rolebinding.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader", calls)

    def test_install_rolls_back_only_personal_server_binding_when_monitoring_binding_preexists(self):
        result, calls = self.run_tool(
            "sre-telegram-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *describe*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\nalertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'apply --dry-run=client'*) exit 0;;\n"
                "  *'monitoring-RoleBinding-sre-telegram-relay-workload-reader.yaml'*) printf 'Error from server (AlreadyExists): rolebindings.rbac.authorization.k8s.io \\\"sre-telegram-relay-workload-reader\\\" already exists\\n'; exit 1;;\n"
                "  *'personal-server-RoleBinding-sre-telegram-relay-workload-reader.yaml'*) printf 'rolebinding.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader created\\n'; exit 0;;\n"
                "  *'monitoring-Deployment-sre-telegram-relay.yaml'*) printf 'Error from server (InternalError): later resource create failed\\n'; exit 1;;\n"
                "  *'images list'*) printf 'REF TYPE DIGEST SIZE PLATFORMS LABELS\\npersonal-server-sre-telegram-relay:latest x sha256:abc 1MB linux/amd64 -\\n'; exit 0;;\n"
                "  *delete*) exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
                "docker": "#!/bin/sh\n"
                "case \"$1\" in build) exit 0;; save) printf image-stream; exit 0;; esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"SRE_TELEGRAM_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_install=FAIL"))
        self.assertIn("-n personal-server delete rolebinding.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader", calls)
        self.assertNotIn("-n monitoring delete rolebinding.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader", calls)

    def test_install_handles_cluster_role_binding_as_cluster_scoped_during_create_and_rollback(self):
        result, calls = self.run_tool(
            "sre-telegram-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *describe*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\nalertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'apply --dry-run=client'*) exit 0;;\n"
                "  *'ClusterRoleBinding-sre-telegram-relay-node-reader.yaml'*) printf 'clusterrolebinding.rbac.authorization.k8s.io/sre-telegram-relay-node-reader created\\n'; exit 0;;\n"
                "  *'monitoring-Deployment-sre-telegram-relay.yaml'*) printf 'Error from server (InternalError): later resource create failed\\n'; exit 1;;\n"
                "  *'images list'*) printf 'REF TYPE DIGEST SIZE PLATFORMS LABELS\\npersonal-server-sre-telegram-relay:latest x sha256:abc 1MB linux/amd64 -\\n'; exit 0;;\n"
                "  *delete*) exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
                "docker": "#!/bin/sh\n"
                "case \"$1\" in build) exit 0;; save) printf image-stream; exit 0;; esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"SRE_TELEGRAM_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_install=FAIL"))
        cluster_binding = "clusterrolebinding.rbac.authorization.k8s.io/sre-telegram-relay-node-reader"
        cluster_manifest = "ClusterRoleBinding-sre-telegram-relay-node-reader.yaml"
        cluster_create_calls = [
            line for line in calls.splitlines() if cluster_manifest in line and " create -f " in line
        ]
        self.assertTrue(cluster_create_calls)
        self.assertTrue(all(" kubectl create -f " in line for line in cluster_create_calls))
        self.assertIn(f"sudo k3s kubectl delete {cluster_binding} --ignore-not-found", calls)
        self.assertNotIn(f"-n monitoring delete {cluster_binding}", calls)

    def test_install_refuses_preexisting_relay_resources_before_helm_upgrade(self):
        result, calls = self.run_tool(
            "sre-telegram-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *describe*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\nalertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'apply --dry-run=client'*) exit 0;;\n"
                "  *'monitoring-ConfigMap-sre-telegram-relay-state.yaml'*) printf 'configmap/sre-telegram-relay-state already exists\\n'; exit 1;;\n"
                "  *'images list'*) printf 'REF TYPE DIGEST SIZE PLATFORMS LABELS\\npersonal-server-sre-telegram-relay:latest x sha256:abc 1MB linux/amd64 -\\n'; exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
                "docker": "#!/bin/sh\n"
                "case \"$1\" in build) exit 0;; save) printf image-stream; exit 0;; esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"SRE_TELEGRAM_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_install=FAIL"))
        self.assertIn("create -f", calls)
        self.assertNotIn("helm upgrade", calls)

    def test_install_signal_runs_cleanup_and_helm_rollback(self):
        result, calls = self.run_tool(
            "sre-telegram-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *describe*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\nalertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'apply --dry-run=client'*) exit 0;;\n"
                "  *'RoleBinding-sre-telegram-relay-workload-reader.yaml'*) printf 'rolebinding.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader created\\n'; exit 0;;\n"
                "  *delete*) exit 0;;\n"
                "  *'images list'*) printf 'REF TYPE DIGEST SIZE PLATFORMS LABELS\\npersonal-server-sre-telegram-relay:latest x sha256:abc 1MB linux/amd64 -\\n'; exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
                "docker": "#!/bin/sh\n"
                "case \"$1\" in build) exit 0;; save) printf image-stream; exit 0;; esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$1\" in\n"
                "  template) exit 0;;\n"
                "  status) printf '{\"info\":{\"status\":\"deployed\",\"revision\":\"2\"}}\\n'; exit 0;;\n"
                "  upgrade) kill -TERM \"$PPID\"; sleep 1; exit 0;;\n"
                "  rollback) exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
            },
            env_overrides={"SRE_TELEGRAM_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertEqual(result.returncode, 130)
        self.assertIn("helm rollback personal-server-monitoring 2 --namespace monitoring", calls)
        self.assertIn("-n personal-server delete rolebinding.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader", calls)

    def test_verify_fails_closed_when_rbac_can_i_errors(self):
        result, _ = self.run_tool(
            "sre-telegram-verify.sh",
            stubs={
                "sudo": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'rollout status deployment/sre-telegram-relay'*) exit 0;;\n"
                "  *'get service sre-telegram-relay -o json'*) printf '{\"spec\":{\"type\":\"ClusterIP\",\"ports\":[{\"port\":8080}]}}\\n'; exit 0;;\n"
                "  *'get prometheusrule sre-telegram-k3s-alerts'*) exit 0;;\n"
                "  *'auth can-i'*) exit 1;;\n"
                "  *'port-forward'*) sleep 30;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "curl": "#!/bin/sh\nprintf 'ok\\n'\nexit 0\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_verify=FAIL"))

    def test_verify_rejects_any_down_active_prometheus_target(self):
        result, _ = self.run_tool(
            "sre-telegram-verify.sh",
            stubs={
                "sudo": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'rollout status deployment/sre-telegram-relay'*) exit 0;;\n"
                "  *'get service sre-telegram-relay -o json'*) printf '{\"spec\":{\"type\":\"ClusterIP\",\"ports\":[{\"port\":8080}]}}\\n'; exit 0;;\n"
                "  *'get prometheusrule sre-telegram-k3s-alerts'*) exit 0;;\n"
                "  *'auth can-i'*) printf 'no\\n'; exit 0;;\n"
                "  *'port-forward'*) sleep 30;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "curl": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *18080*) printf 'ok\\n';;\n"
                "  *) printf '{\"status\":\"success\",\"data\":{\"activeTargets\":[{\"health\":\"up\"},{\"health\":\"down\"}]}}\\n';;\n"
                "esac\n"
                "exit 0\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_verify=FAIL"))

    def test_verify_rejects_external_service_exposure_drift_and_uses_existing_prometheus_service(self):
        result, calls = self.run_tool(
            "sre-telegram-verify.sh",
            stubs={
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'rollout status deployment/sre-telegram-relay'*) exit 0;;\n"
                "  *'get service sre-telegram-relay -o json'*) printf '{\"spec\":{\"type\":\"ClusterIP\",\"externalIPs\":[\"10.0.0.5\"],\"ports\":[{\"port\":8080}]}}\\n'; exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_verify=FAIL"))
        self.assertNotIn("kube-prometheus-prometheus", calls)

    def test_verify_passes_only_when_all_rbac_denials_and_targets_are_verified(self):
        result, calls = self.run_tool(
            "sre-telegram-verify.sh",
            stubs={
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'rollout status deployment/sre-telegram-relay'*) exit 0;;\n"
                "  *'get service sre-telegram-relay -o json'*) printf '{\"spec\":{\"type\":\"ClusterIP\",\"ports\":[{\"port\":8080}]}}\\n'; exit 0;;\n"
                "  *'get prometheusrule sre-telegram-k3s-alerts'*) exit 0;;\n"
                "  *'auth can-i'*) printf 'no\\n'; exit 0;;\n"
                "  *'port-forward'*) sleep 30;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "curl": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *18080*) printf 'ok\\n';;\n"
                "  *) printf '{\"status\":\"success\",\"data\":{\"activeTargets\":[{\"health\":\"up\"},{\"health\":\"up\"}]}}\\n';;\n"
                "esac\n"
                "exit 0\n",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_verify=PASS"))
        self.assertIn("service/personal-server-monitoring-prometheus", calls)
        self.assertGreaterEqual(calls.count("auth can-i"), 12)


if __name__ == "__main__":
    unittest.main()
