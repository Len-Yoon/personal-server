import unittest
from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "infra" / "k8s" / "backup-automation" / "portal-pvc-backup-cronjob.yaml"
DOCKERFILE = ROOT / "infra" / "k8s" / "backup-automation" / "Dockerfile"


def documents():
    with MANIFEST.open(encoding="utf-8") as stream:
        return [doc for doc in yaml.safe_load_all(stream) if doc]


def find(kind, name, namespace=None):
    for doc in documents():
        metadata = doc.get("metadata", {})
        if doc.get("kind") == kind and metadata.get("name") == name:
            if namespace is None or metadata.get("namespace") == namespace:
                return doc
    raise AssertionError(f"missing {kind}/{name}")


def cronjob():
    return find("CronJob", "portal-pvc-backup", "personal-server")


def pod_spec():
    return cronjob()["spec"]["jobTemplate"]["spec"]["template"]["spec"]


class PortalPvcBackupCronJobTests(unittest.TestCase):
    def test_cronjob_is_suspended_singleton_and_never_retries(self):
        job = cronjob()
        self.assertTrue(job["spec"]["suspend"])
        self.assertEqual(job["spec"]["concurrencyPolicy"], "Forbid")
        self.assertEqual(job["spec"]["jobTemplate"]["spec"]["backoffLimit"], 0)
        self.assertEqual(pod_spec()["restartPolicy"], "Never")

    def test_cronjob_runs_only_verifier_go_command(self):
        container = pod_spec()["containers"][0]
        self.assertEqual(container["command"], ["/opt/personal-server/portal-pvc-backup-verify.sh", "--go"])

    def test_backup_secret_is_file_only_with_fixed_keys(self):
        spec = pod_spec()
        self.assertNotIn("envFrom", spec["containers"][0])
        for item in spec["containers"][0].get("env", []):
            self.assertNotIn("valueFrom", item)
        secret_volumes = [v for v in spec["volumes"] if "secret" in v]
        self.assertEqual(len(secret_volumes), 1)
        secret = secret_volumes[0]["secret"]
        self.assertEqual(secret["secretName"], "portal-pvc-backup-runtime")
        self.assertTrue(secret["optional"] is False)
        self.assertEqual(
            {item["key"] for item in secret["items"]},
            {"rclone-config", "rclone-config-passphrase", "age-recipient", "age-identity"},
        )
        self.assertTrue(any(m["name"] == secret_volumes[0]["name"] and m["readOnly"] for m in spec["containers"][0]["volumeMounts"]))

    def test_pod_mounts_both_portal_pvcs_read_only(self):
        mounts = {m["name"]: m for m in pod_spec()["containers"][0]["volumeMounts"]}
        self.assertEqual(mounts["portal-files"]["mountPath"], "/data/files")
        self.assertEqual(mounts["portal-state"]["mountPath"], "/data/portal-web-state")
        self.assertTrue(mounts["portal-files"]["readOnly"])
        self.assertTrue(mounts["portal-state"]["readOnly"])
        volumes = {v["name"]: v for v in pod_spec()["volumes"]}
        self.assertEqual(volumes["portal-files"]["persistentVolumeClaim"]["claimName"], "portal-web-files-dynamic")
        self.assertEqual(volumes["portal-state"]["persistentVolumeClaim"]["claimName"], "portal-web-state-dynamic")

    def test_service_account_and_rbac_have_only_fixed_minimum_permissions(self):
        self.assertEqual(find("ServiceAccount", "portal-pvc-backup", "personal-server")["metadata"]["name"], "portal-pvc-backup")
        roles = [doc for doc in documents() if doc.get("kind") == "Role"]
        rules = [rule for role in roles for rule in role["rules"]]
        self.assertFalse(any("secrets" in rule.get("resources", []) for rule in rules))
        self.assertFalse(any("pods/exec" in rule.get("resources", []) for rule in rules))
        for rule in rules:
            self.assertNotIn("create", rule.get("verbs", []))
            self.assertNotIn("delete", rule.get("verbs", []))
        scale = [r for r in rules if r.get("resources") == ["deployments/scale"]]
        self.assertEqual(scale[0]["resourceNames"], ["portal-web"])
        self.assertEqual(set(scale[0]["verbs"]), {"get", "patch"})
        pvc = [r for r in rules if r.get("resources") == ["persistentvolumeclaims"]]
        self.assertEqual(pvc[0]["resourceNames"], ["portal-web-files-dynamic", "portal-web-state-dynamic"])

    def test_monitoring_status_rolebinding_targets_the_fixed_runner_service_account(self):
        binding = find("RoleBinding", "portal-pvc-backup-status", "monitoring")
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": "portal-pvc-backup-status",
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount",
            "name": "portal-pvc-backup",
            "namespace": "personal-server",
        }])

    def test_rbac_allows_only_rollout_watch_and_fixed_runner_resources(self):
        """The rollout wait needs watch, while every resource name remains fixed."""
        actual = {
            (role["metadata"]["namespace"], tuple(rule["resources"]), tuple(rule.get("resourceNames", []))): set(rule["verbs"])
            for role in (doc for doc in documents() if doc.get("kind") == "Role")
            for rule in role["rules"]
        }
        self.assertEqual(
            actual,
            {
                ("personal-server", ("persistentvolumeclaims",), ("portal-web-files-dynamic", "portal-web-state-dynamic")): {"get"},
                ("personal-server", ("deployments",), ("portal-web",)): {"get", "watch"},
                ("personal-server", ("deployments/scale",), ("portal-web",)): {"get", "patch"},
                ("personal-server", ("configmaps",), ("portal-pvc-backup-evidence",)): {"get", "patch"},
                ("monitoring", ("configmaps",), ("sre-telegram-backup-status",)): {"get", "patch"},
            },
        )

    def test_manifest_does_not_overwrite_existing_telegram_or_evidence_configmaps(self):
        """The installer creates missing non-secret keys without applying over live status."""
        configmaps = [doc for doc in documents() if doc.get("kind") == "ConfigMap"]
        self.assertEqual(configmaps, [])

    def test_secret_projection_and_emptydirs_are_securely_writable_by_the_nonroot_runner(self):
        """The pod must read projected credentials while retaining only bounded writable scratch space."""
        spec = pod_spec()
        self.assertEqual(spec["securityContext"]["fsGroup"], 10001)
        self.assertEqual(spec["securityContext"]["fsGroupChangePolicy"], "OnRootMismatch")
        volumes = {volume["name"]: volume for volume in spec["volumes"]}
        self.assertEqual(volumes["backup-runtime"]["secret"]["defaultMode"], 0o440)
        self.assertEqual(volumes["work"]["emptyDir"]["sizeLimit"], "10Gi")
        self.assertEqual(volumes["tmp"]["emptyDir"]["sizeLimit"], "1Gi")
        environment = {item["name"]: item["value"] for item in spec["containers"][0]["env"]}
        self.assertEqual(environment["TMPDIR"], "/work")
        resources = spec["containers"][0]["resources"]
        self.assertEqual(resources["requests"]["ephemeral-storage"], "1Gi")
        self.assertEqual(resources["limits"]["ephemeral-storage"], "11Gi")

    def test_cronjob_uses_kst_retention_and_a_grace_period_for_portal_restore(self):
        """An interrupted backup needs time to restore the writer and must not retain Jobs indefinitely."""
        job_spec = cronjob()["spec"]
        pod = pod_spec()
        self.assertEqual(job_spec["timeZone"], "Asia/Seoul")
        self.assertEqual(job_spec["jobTemplate"]["spec"]["ttlSecondsAfterFinished"], 86400)
        self.assertGreaterEqual(pod["terminationGracePeriodSeconds"], 300)

    def test_container_is_non_root_read_only_and_capability_restricted(self):
        spec = pod_spec()
        security = spec["containers"][0]["securityContext"]
        self.assertTrue(security["runAsNonRoot"])
        self.assertTrue(security["readOnlyRootFilesystem"])
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertEqual(security["capabilities"]["drop"], ["ALL"])

    def test_runner_image_uses_digest_pinned_base_and_bundles_verifier(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertRegex(
            text,
            re.compile(
                r"^FROM python:3\.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534$",
                re.MULTILINE,
            ),
        )
        self.assertRegex(text, r"apt-get install[^\n]*\bkubernetes-client\b")
        self.assertIn("groupadd --system --gid 10001 portal-backup", text)
        self.assertRegex(text, r"useradd[^\n]*--uid 10001[^\n]*--gid portal-backup")
        self.assertIn("portal-pvc-backup-verify.sh", text)
        self.assertIn("validate-backup-evidence.py", text)


if __name__ == "__main__":
    unittest.main()
