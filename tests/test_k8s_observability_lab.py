"""Contracts for the isolated, sample-only Loki observability lab."""

import json
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "infra/k8s/observability-lab/loki-lab.yaml"
NAMESPACE = "observability-lab"
MONITORING = ROOT / "infra/k8s/monitoring"


class LokiLabManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents = ([doc for doc in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")) if doc]
                         if MANIFEST.is_file() else [])

    def find(self, kind, name):
        matches = [doc for doc in self.documents if doc["kind"] == kind and doc["metadata"]["name"] == name]
        if not matches:
            self.fail(f"Missing {kind}/{name} in {MANIFEST}")
        self.assertEqual(len(matches), 1, (kind, name))
        return matches[0]

    def test_every_resource_is_named_and_isolated(self):
        self.assertEqual({(doc["kind"], doc["metadata"]["name"]) for doc in self.documents}, {
            ("Namespace", NAMESPACE),
            ("ConfigMap", "loki-lab"), ("PersistentVolumeClaim", "loki-lab-data"),
            ("Deployment", "loki-lab"), ("Service", "loki-lab"),
            ("ConfigMap", "loki-lab-alloy"), ("ServiceAccount", "loki-lab-alloy"),
            ("Role", "loki-lab-alloy"), ("RoleBinding", "loki-lab-alloy"),
            ("Deployment", "loki-lab-alloy"), ("Deployment", "loki-lab-sample"),
            ("NetworkPolicy", "loki-lab-ingress"),
        })
        for doc in self.documents:
            if doc["kind"] != "Namespace":
                self.assertEqual(doc["metadata"]["namespace"], NAMESPACE)
        self.assertEqual(self.find("Namespace", NAMESPACE)["metadata"]["name"], NAMESPACE)

    def test_loki_storage_retention_and_internal_service_are_bounded(self):
        pvc_document = self.find("PersistentVolumeClaim", "loki-lab-data")
        pvc = pvc_document["spec"]
        self.assertEqual(pvc["storageClassName"], "local-path")
        self.assertEqual(pvc["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(pvc["resources"]["requests"]["storage"], "1Gi")
        self.assertIn("not a host disk quota", pvc_document["metadata"].get("annotations", {}).get("storage-boundary", ""))
        loki_configmap = self.find("ConfigMap", "loki-lab")
        self.assertIn("not a disk usage limit", loki_configmap["metadata"].get("annotations", {}).get("retention-boundary", ""))
        config = yaml.safe_load(loki_configmap["data"]["loki.yaml"])
        self.assertFalse(config["auth_enabled"])
        self.assertEqual(config["common"]["replication_factor"], 1)
        self.assertEqual(config["schema_config"]["configs"][0]["store"], "tsdb")
        self.assertEqual(config["schema_config"]["configs"][0]["object_store"], "filesystem")
        self.assertEqual(config["schema_config"]["configs"][0]["index"]["period"], "24h")
        self.assertEqual(config["limits_config"]["retention_period"], "24h")
        self.assertTrue(config["compactor"]["retention_enabled"])
        self.assertEqual(config["compactor"]["delete_request_store"], "filesystem")
        service = self.find("Service", "loki-lab")["spec"]
        self.assertEqual(service["type"], "ClusterIP")
        self.assertEqual(service["ports"], [{"name": "http", "port": 3100, "targetPort": "http"}])
        self.assertNotIn("externalIPs", service)

    def test_unauthenticated_loki_ingress_only_accepts_alloy_and_grafana(self):
        policy = self.find("NetworkPolicy", "loki-lab-ingress")["spec"]
        self.assertEqual(policy["podSelector"], {"matchLabels": {"app.kubernetes.io/name": "loki-lab"}})
        self.assertEqual(policy["policyTypes"], ["Ingress"])
        self.assertEqual(policy["ingress"], [{
            "from": [
                {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "loki-lab-alloy"}}},
                {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}},
                    "podSelector": {"matchLabels": {
                        "app.kubernetes.io/name": "grafana",
                        "app.kubernetes.io/instance": "personal-server-monitoring",
                    }},
                },
            ],
            "ports": [{"protocol": "TCP", "port": 3100}],
        }])
        self.assertNotIn("egress", policy)  # DNS, K8s API, and Loki egress remain available.

    def test_collector_rbac_reads_only_local_pods_and_logs(self):
        role = self.find("Role", "loki-lab-alloy")
        self.assertEqual(role["rules"], [
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
            {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
        ])
        binding = self.find("RoleBinding", "loki-lab-alloy")
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "loki-lab-alloy",
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount", "name": "loki-lab-alloy", "namespace": NAMESPACE,
        }])
        self.assertEqual(self.find("ServiceAccount", "loki-lab-alloy")["automountServiceAccountToken"], True)

    def test_alloy_discovers_only_the_sample_and_writes_to_internal_loki(self):
        config = self.find("ConfigMap", "loki-lab-alloy")["data"]["config.alloy"]
        for required in (
            'discovery.kubernetes "sample"', 'role = "pod"',
            'names = ["observability-lab"]',
            'label = "app.kubernetes.io/name=loki-lab-sample"',
            'discovery.relabel "sample"',
            'target_label = "app"',
            'loki.source.kubernetes "sample"', 'loki.write "lab"',
            'http://loki-lab.observability-lab.svc.cluster.local:3100/loki/api/v1/push',
        ):
            self.assertIn(required, config)
        self.assertEqual(config.count('discovery.kubernetes "'), 1)
        self.assertEqual(config.count('loki.source.kubernetes "'), 1)

    def test_workloads_have_small_resources_and_restricted_security(self):
        expected = {
            "loki-lab": ({"cpu": "100m", "memory": "128Mi"}, {"cpu": "500m", "memory": "512Mi"}),
            "loki-lab-alloy": ({"cpu": "50m", "memory": "64Mi"}, {"cpu": "200m", "memory": "256Mi"}),
            "loki-lab-sample": ({"cpu": "10m", "memory": "16Mi"}, {"cpu": "50m", "memory": "64Mi"}),
        }
        for name, (requests, limits) in expected.items():
            with self.subTest(name=name):
                deployment = self.find("Deployment", name)
                self.assertEqual(deployment["spec"]["replicas"], 1)
                pod = deployment["spec"]["template"]["spec"]
                self.assertFalse(pod.get("hostNetwork", False))
                self.assertFalse(pod.get("hostPID", False))
                self.assertIn("hostIPC", pod)
                self.assertFalse(pod["hostIPC"])
                self.assertFalse(pod.get("initContainers"))
                self.assertFalse(pod.get("ephemeralContainers"))
                self.assertEqual(pod["automountServiceAccountToken"], name == "loki-lab-alloy")
                self.assertEqual(pod["securityContext"]["runAsNonRoot"], True)
                self.assertGreater(pod["securityContext"]["runAsUser"], 0)
                self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
                self.assertEqual(len(pod["containers"]), 1)
                container = pod["containers"][0]
                self.assertEqual(container["resources"], {"requests": requests, "limits": limits})
                self.assertEqual(container["securityContext"], {
                    "allowPrivilegeEscalation": False,
                    "privileged": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                })
                self.assertNotIn("hostPath", str(pod))
                self.assertNotIn("secret", str(pod).lower())
                self.assertTrue(all(set(volume) <= {"name", "configMap", "persistentVolumeClaim", "emptyDir"} for volume in pod.get("volumes", [])))
        loki = self.find("Deployment", "loki-lab")["spec"]["template"]["spec"]
        self.assertEqual(loki["containers"][0]["image"], "grafana/loki:3.7.0")
        self.assertEqual(loki["serviceAccountName"], "default")
        self.assertEqual(loki["volumes"][1]["persistentVolumeClaim"], {"claimName": "loki-lab-data"})
        self.assertEqual(self.find("Deployment", "loki-lab-sample")["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/name"], "loki-lab-sample")
        sample = self.find("Deployment", "loki-lab-sample")["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(sample["command"], ["/bin/sh", "-c", "while true; do echo 'loki lab sample ready'; sleep 30; done"])


class LokiLabGrafanaTests(unittest.TestCase):
    def test_grafana_datasource_sidecar_is_explicitly_enabled(self):
        values = yaml.safe_load((MONITORING / "values.n100.yaml").read_text(encoding="utf-8"))
        self.assertEqual(values["grafana"]["sidecar"]["datasources"], {
            "enabled": True, "label": "grafana_datasource", "labelValue": "1",
        })

    def test_loki_datasource_is_provisioned_as_nondefault_internal_proxy(self):
        path = MONITORING / "loki-lab-datasource.yaml"
        self.assertTrue(path.is_file(), path)
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["kind"], "ConfigMap")
        self.assertEqual(manifest["metadata"]["namespace"], "monitoring")
        self.assertEqual(manifest["metadata"]["labels"], {"grafana_datasource": "1"})
        self.assertEqual(len(manifest["data"]), 1)
        provisioning = yaml.safe_load(next(iter(manifest["data"].values())))
        self.assertEqual(provisioning["apiVersion"], 1)
        self.assertEqual(provisioning["datasources"], [{
            "name": "Loki Lab", "uid": "loki-lab", "type": "loki",
            "access": "proxy", "url": "http://loki-lab.observability-lab.svc.cluster.local:3100",
            "isDefault": False, "editable": False,
        }])

    def test_loki_dashboard_queries_only_sample_logs_with_fixed_line_format(self):
        path = MONITORING / "loki-lab-dashboard.yaml"
        self.assertTrue(path.is_file(), path)
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["kind"], "ConfigMap")
        self.assertEqual(manifest["metadata"]["namespace"], "monitoring")
        self.assertEqual(manifest["metadata"]["labels"], {"grafana_dashboard": "1"})
        self.assertEqual(len(manifest["data"]), 1)
        dashboard = json.loads(next(iter(manifest["data"].values())))
        self.assertEqual(dashboard["uid"], "loki-lab-sample")
        self.assertEqual(len(dashboard["panels"]), 1)
        panel = dashboard["panels"][0]
        self.assertEqual(panel["type"], "logs")
        self.assertEqual(panel["datasource"], {"type": "loki", "uid": "loki-lab"})
        self.assertEqual(panel["targets"], [{
            "datasource": {"type": "loki", "uid": "loki-lab"},
            "expr": '{app="loki-lab-sample"} | line_format "{{ __line__ }}"',
            "refId": "A",
        }])
