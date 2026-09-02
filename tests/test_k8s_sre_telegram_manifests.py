import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_ROOT = ROOT / "infra" / "k8s" / "sre-telegram"


def load_yaml_documents(filename: str) -> list[dict]:
    path = MANIFEST_ROOT / filename
    if not path.is_file():
        raise AssertionError(f"required manifest is missing: {path.relative_to(ROOT)}")
    with path.open(encoding="utf-8") as manifest:
        return [document for document in yaml.safe_load_all(manifest) if document]


def find_document(documents: list[dict], kind: str, name: str) -> dict:
    return next(
        document
        for document in documents
        if document["kind"] == kind and document["metadata"]["name"] == name
    )


class SreTelegramManifestContractTests(unittest.TestCase):
    def test_relay_service_is_cluster_ip_without_published_node_port(self):
        service = find_document(load_yaml_documents("base.yaml"), "Service", "sre-telegram-relay")

        self.assertEqual(service["metadata"]["namespace"], "monitoring")
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        self.assertEqual(service["spec"]["ports"], [{"name": "http", "port": 8080, "targetPort": "http"}])
        self.assertNotIn("nodePort", service["spec"])
        self.assertNotIn("loadBalancerIP", service["spec"])

    def test_relay_runs_hardened_single_replica_with_mounted_secret_keys(self):
        deployment = find_document(load_yaml_documents("base.yaml"), "Deployment", "sre-telegram-relay")
        pod_spec = deployment["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]

        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(pod_spec["serviceAccountName"], "sre-telegram-relay")
        self.assertTrue(pod_spec["securityContext"]["runAsNonRoot"])
        self.assertEqual(pod_spec["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertEqual(container["imagePullPolicy"], "Never")
        self.assertEqual(container["readinessProbe"]["httpGet"], {"path": "/healthz", "port": "http"})
        self.assertEqual(container["livenessProbe"]["httpGet"], {"path": "/healthz", "port": "http"})

        runtime_volume = next(volume for volume in pod_spec["volumes"] if volume["name"] == "relay-runtime")
        self.assertEqual(runtime_volume["secret"]["secretName"], "sre-telegram-relay-runtime")
        self.assertEqual(
            {item["key"] for item in runtime_volume["secret"]["items"]},
            {"telegram_bot_token", "allowed_chat_id", "alertmanager_auth_token"},
        )
        self.assertEqual(
            {item["name"]: item["value"] for item in container["env"]},
            {
                "TELEGRAM_BOT_TOKEN_FILE": "/var/run/sre-telegram-relay/telegram_bot_token",
                "ALLOWED_CHAT_ID_FILE": "/var/run/sre-telegram-relay/allowed_chat_id",
                "ALERTMANAGER_AUTH_TOKEN_FILE": "/var/run/sre-telegram-relay/alertmanager_auth_token",
            },
        )

    def test_rbac_is_read_only_except_named_relay_state_configmap(self):
        documents = load_yaml_documents("base.yaml")
        state_role = find_document(documents, "Role", "sre-telegram-relay-state")
        node_role = find_document(documents, "ClusterRole", "sre-telegram-relay-node-reader")
        workload_role = find_document(documents, "ClusterRole", "sre-telegram-relay-workload-reader")

        self.assertEqual(
            state_role["rules"],
            [{
                "apiGroups": [""],
                "resources": ["configmaps"],
                "resourceNames": ["sre-telegram-relay-state"],
                "verbs": ["get", "update", "patch"],
            }],
        )
        self.assertEqual(
            node_role["rules"],
            [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list", "watch"]}],
        )
        self.assertEqual(
            workload_role["rules"],
            [
                {
                    "apiGroups": [""],
                    "resources": ["pods", "persistentvolumeclaims"],
                    "verbs": ["get", "list", "watch"],
                },
                {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list", "watch"]},
            ],
        )
        bindings = [document for document in documents if document["kind"].endswith("Binding")]
        self.assertEqual({binding["metadata"]["namespace"] for binding in bindings if binding["kind"] == "RoleBinding"}, {"monitoring", "personal-server"})
        for binding in bindings:
            self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": "sre-telegram-relay", "namespace": "monitoring"}])

    def test_manifest_whitelists_every_rbac_role_and_binding(self):
        documents = load_yaml_documents("base.yaml")
        rbac_roles = [document for document in documents if document["kind"] in {"Role", "ClusterRole"}]
        rbac_bindings = [document for document in documents if document["kind"] in {"RoleBinding", "ClusterRoleBinding"}]
        expected_roles = {
            ("ClusterRole", "sre-telegram-relay-node-reader"): [
                {"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list", "watch"]}
            ],
            ("ClusterRole", "sre-telegram-relay-workload-reader"): [
                {
                    "apiGroups": [""],
                    "resources": ["pods", "persistentvolumeclaims"],
                    "verbs": ["get", "list", "watch"],
                },
                {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list", "watch"]},
            ],
            ("Role", "sre-telegram-relay-state"): [
                {
                    "apiGroups": [""],
                    "resources": ["configmaps"],
                    "resourceNames": ["sre-telegram-relay-state"],
                    "verbs": ["get", "update", "patch"],
                }
            ],
        }
        expected_bindings = {
            ("ClusterRoleBinding", "sre-telegram-relay-node-reader", None): {
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "sre-telegram-relay-node-reader"},
                "subjects": [{"kind": "ServiceAccount", "name": "sre-telegram-relay", "namespace": "monitoring"}],
            },
            ("RoleBinding", "sre-telegram-relay-workload-reader", "monitoring"): {
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "sre-telegram-relay-workload-reader"},
                "subjects": [{"kind": "ServiceAccount", "name": "sre-telegram-relay", "namespace": "monitoring"}],
            },
            ("RoleBinding", "sre-telegram-relay-workload-reader", "personal-server"): {
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "sre-telegram-relay-workload-reader"},
                "subjects": [{"kind": "ServiceAccount", "name": "sre-telegram-relay", "namespace": "monitoring"}],
            },
            ("RoleBinding", "sre-telegram-relay-state", "monitoring"): {
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "sre-telegram-relay-state"},
                "subjects": [{"kind": "ServiceAccount", "name": "sre-telegram-relay", "namespace": "monitoring"}],
            },
        }

        self.assertEqual(len(rbac_roles), len(expected_roles))
        self.assertEqual(
            {(document["kind"], document["metadata"]["name"]): document["rules"] for document in rbac_roles},
            expected_roles,
        )
        self.assertEqual(len(rbac_bindings), len(expected_bindings))
        self.assertEqual(
            {
                (document["kind"], document["metadata"]["name"], document["metadata"].get("namespace")): {
                    "roleRef": document["roleRef"],
                    "subjects": document["subjects"],
                }
                for document in rbac_bindings
            },
            expected_bindings,
        )
        for document in rbac_roles:
            for rule in document["rules"]:
                self.assertTrue({"secrets", "pods/exec"}.isdisjoint(rule["resources"]))
                self.assertNotIn("delete", rule["verbs"])
                if "deployments" in rule["resources"]:
                    self.assertNotIn("patch", rule["verbs"])

    def test_rules_cover_restart_deployment_pvc_and_target_failures(self):
        rule = find_document(load_yaml_documents("prometheus-rule.yaml"), "PrometheusRule", "sre-telegram-k3s-alerts")
        rules = [item for group in rule["spec"]["groups"] for item in group["rules"]]
        expected_rules = [
            {
                "alert": "PodRestartIncrease",
                "expr": 'increase(kube_pod_container_status_restarts_total{namespace=~"monitoring|personal-server"}[15m]) > 3',
                "for": "15m",
                "labels": {"severity": "warning", "sre_telegram": "true"},
            },
            {
                "alert": "DeploymentUnavailable",
                "expr": 'kube_deployment_spec_replicas{namespace=~"monitoring|personal-server"} > kube_deployment_status_replicas_available{namespace=~"monitoring|personal-server"}',
                "for": "10m",
                "labels": {"severity": "warning", "sre_telegram": "true"},
            },
            {
                "alert": "PVCNotBound",
                "expr": 'kube_persistentvolumeclaim_status_phase{namespace=~"monitoring|personal-server",phase="Bound"} == 0',
                "for": "10m",
                "labels": {"severity": "warning", "sre_telegram": "true"},
            },
            {
                "alert": "PrometheusTargetDown",
                "expr": "up == 0",
                "for": "5m",
                "labels": {"severity": "warning", "sre_telegram": "true"},
            },
        ]

        self.assertEqual(len(rules), 4)
        self.assertEqual(
            [{key: item[key] for key in ("alert", "expr", "for", "labels")} for item in rules],
            expected_rules,
        )

    def test_alertmanager_references_existing_secret_not_inline_token(self):
        values_path = MANIFEST_ROOT / "alertmanager-values.yaml"
        self.assertTrue(values_path.is_file(), f"required manifest is missing: {values_path.relative_to(ROOT)}")
        values_text = values_path.read_text(encoding="utf-8")
        values = yaml.safe_load(values_text)

        self.assertTrue(values["alertmanager"]["enabled"])
        self.assertEqual(
            values["alertmanager"]["alertmanagerSpec"]["configSecret"],
            "sre-telegram-alertmanager-config",
        )
        self.assertNotIn("config", values["alertmanager"])
        self.assertNotIn("alertmanagerConfig", values)
        self.assertNotIn("token", values_text.lower())
        self.assertNotIn("Secret", {document["kind"] for document in load_yaml_documents("base.yaml")})
        # Task 3 preflight/seed validation owns the external Secret's route structure without printing values.


if __name__ == "__main__":
    unittest.main()
