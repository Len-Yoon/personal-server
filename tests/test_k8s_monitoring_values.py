import unittest
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

    def test_values_keep_prometheus_retention_and_local_storage(self):
        spec = load_values()["prometheus"]["prometheusSpec"]

        self.assertEqual(spec["retention"], "7d")
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

    def test_values_set_kube_state_metrics_resources(self):
        resources = load_values()["kube-state-metrics"]["resources"]

        self.assertIn("requests", resources)
        self.assertIn("limits", resources)
        self.assertTrue(resources["requests"])
        self.assertTrue(resources["limits"])


if __name__ == "__main__":
    unittest.main()
