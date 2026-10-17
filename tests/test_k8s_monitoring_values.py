import unittest
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_values():
    values_path = ROOT / "infra/k8s/monitoring/values.n100.yaml"
    return yaml.safe_load(values_path.read_text())


class MonitoringValuesContractTests(unittest.TestCase):
    def test_values_keep_grafana_internal_and_persistent(self):
        values = load_values()

        self.assertEqual(values["grafana"]["service"]["type"], "ClusterIP")
        self.assertIs(values["grafana"]["ingress"]["enabled"], False)
        self.assertIs(values["grafana"]["persistence"]["enabled"], True)
        self.assertEqual(
            values["grafana"]["persistence"]["storageClassName"], "local-path"
        )
        self.assertEqual(values["grafana"]["persistence"]["size"], "1Gi")
        self.assertEqual(
            values["grafana"]["persistence"]["annotations"]["helm.sh/resource-policy"],
            "keep",
        )

    def test_values_reserve_enough_memory_for_grafana_runtime(self):
        resources = load_values()["grafana"]["resources"]

        self.assertEqual(resources["requests"]["memory"], "256Mi")
        self.assertEqual(resources["limits"]["memory"], "512Mi")

    def test_values_keep_prometheus_retention_and_local_storage(self):
        spec = load_values()["prometheus"]["prometheusSpec"]

        self.assertEqual(spec["retention"], "7d")
        self.assertEqual(spec["retentionSize"], "3GB")
        # Prometheus uses binary GB; leave 40% of the request for WAL/head/compaction.
        self.assertLessEqual(3 * 1024**3, 0.6 * 5 * 1024**3)
        self.assertEqual(
            spec["storageSpec"]["volumeClaimTemplate"]["spec"]["storageClassName"],
            "local-path",
        )
        self.assertEqual(
            spec["storageSpec"]["volumeClaimTemplate"]["spec"]["resources"][
                "requests"
            ]["storage"],
            "5Gi",
        )
        self.assertEqual(
            spec["storageSpec"]["volumeClaimTemplate"]["metadata"]["annotations"][
                "helm.sh/resource-policy"
            ],
            "keep",
        )

    def test_disk_rules_evaluate_threshold_duration_and_recovery_with_promtool(self):
        promtool = os.environ.get("PROMTOOL") or shutil.which("promtool")
        if not promtool:
            self.skipTest("promtool is required to execute PromQL rule fixtures")
        manifest = yaml.safe_load((ROOT / "infra/k8s/sre-telegram/prometheus-rule.yaml").read_text())
        rules = [rule for group in manifest["spec"]["groups"] for rule in group["rules"]
                 if rule["alert"] in {"NodeDiskPressure", "PVCSpaceLow"}]
        self.assertEqual(len(rules), 2)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "disk-alerts.rules.yaml").write_text(yaml.safe_dump({"groups": [{"name": "disk", "rules": rules}]}, allow_unicode=True))
            fixture = base / "disk-alerts.test.yaml"
            fixture.write_text((ROOT / "tests/fixtures/monitoring-disk-alerts.test.yaml").read_text())
            result = subprocess.run([promtool, "test", "rules", str(fixture)], cwd=base,
                                    capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_values_set_kube_state_metrics_resources(self):
        resources = load_values()["kube-state-metrics"]["resources"]

        self.assertIn("requests", resources)
        self.assertIn("limits", resources)
        self.assertTrue(resources["requests"])
        self.assertTrue(resources["limits"])

    def test_values_disable_node_exporter_for_wsl_mount_propagation(self):
        values = load_values()

        self.assertIs(values["nodeExporter"]["enabled"], False)
        self.assertIs(values["defaultRules"]["rules"]["nodeExporterAlerting"], False)
        self.assertIs(values["defaultRules"]["rules"]["nodeExporterRecording"], False)

    def test_values_disable_only_unavailable_k3s_control_plane_alerts(self):
        values = load_values()
        disabled = values["defaultRules"]["disabled"]

        self.assertEqual(
            disabled,
            {
                "KubeControllerManagerDown": True,
                "KubeSchedulerDown": True,
                "KubeProxyDown": True,
            },
        )
        self.assertNotIn("kubeControllerManager", values)
        self.assertNotIn("kubeScheduler", values)
        self.assertNotIn("kubeProxy", values)


if __name__ == "__main__":
    unittest.main()
