import re
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "infra/k8s/sre-audit-automation/quarterly-sre-audit-cronjob.yaml"
DOCKERFILE = ROOT / "infra/k8s/sre-audit-automation/Dockerfile"
RUNNER = ROOT / "infra/k8s/tools/quarterly-sre-audit-runner.sh"


def documents():
    with MANIFEST.open(encoding="utf-8") as stream:
        return [document for document in yaml.safe_load_all(stream) if document]


def find(kind, name, namespace=None):
    for document in documents():
        metadata = document.get("metadata", {})
        if document.get("kind") == kind and metadata.get("name") == name:
            if namespace is None or metadata.get("namespace") == namespace:
                return document
    raise AssertionError(f"missing {kind}/{name}")


def cronjob():
    return find("CronJob", "quarterly-sre-audit", "monitoring")


def pod_spec():
    return cronjob()["spec"]["jobTemplate"]["spec"]["template"]["spec"]


class QuarterlySreAuditCronJobTests(unittest.TestCase):
    def test_cronjob_has_safe_quarterly_schedule_and_execution_limits(self):
        spec = cronjob()["spec"]
        self.assertEqual(spec["schedule"], "30 3 1 1,4,7,10 *")
        self.assertEqual(spec["timeZone"], "Asia/Seoul")
        self.assertTrue(spec["suspend"])
        self.assertEqual(spec["concurrencyPolicy"], "Forbid")
        self.assertEqual(spec["jobTemplate"]["spec"]["backoffLimit"], 0)
        self.assertGreater(spec["jobTemplate"]["spec"]["activeDeadlineSeconds"], 0)
        self.assertGreater(spec["jobTemplate"]["spec"]["ttlSecondsAfterFinished"], 0)
        self.assertEqual(pod_spec()["restartPolicy"], "Never")

    def test_runner_pod_is_non_root_and_cannot_escalate_privileges(self):
        pod = pod_spec()
        container = pod["containers"][0]
        self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        self.assertTrue(container["securityContext"]["runAsNonRoot"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertNotIn("privileged", container["securityContext"])

    def test_rbac_is_limited_to_audit_reads_fixed_status_and_recovery_lab(self):
        service_account = find("ServiceAccount", "quarterly-sre-audit", "monitoring")
        self.assertTrue(service_account["automountServiceAccountToken"])

        personal_rules = find("Role", "quarterly-sre-audit-read", "personal-server")["rules"]
        self.assertEqual(
            personal_rules,
            [
                {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["portal-pvc-backup-evidence"], "verbs": ["get"]},
                {"apiGroups": ["apps"], "resources": ["deployments"], "resourceNames": ["portal-web"], "verbs": ["get"]},
            ],
        )

        status_rules = find("Role", "quarterly-sre-audit-status", "monitoring")["rules"]
        self.assertEqual(
            status_rules,
            [{"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["sre-telegram-quarterly-audit-status"], "verbs": ["get", "patch"]}],
        )

        lab_rules = find("Role", "quarterly-sre-audit-recovery-lab", "sre-recovery-lab")["rules"]
        self.assertEqual(
            lab_rules,
            [
                {"apiGroups": ["apps"], "resources": ["deployments"], "resourceNames": ["quarterly-sre-recovery-drill"], "verbs": ["delete", "get", "watch"]},
                {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["create"]},
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
                {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create"]},
                {"apiGroups": [""], "resources": ["events"], "verbs": ["list"]},
            ],
        )

        node_rules = find("ClusterRole", "quarterly-sre-audit-nodes")["rules"]
        self.assertEqual(node_rules, [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list"]}])

    def test_manifest_creates_only_the_fixed_recovery_namespace_and_no_sensitive_mounts(self):
        self.assertEqual(find("Namespace", "sre-recovery-lab")["metadata"]["name"], "sre-recovery-lab")
        pod = pod_spec()
        self.assertNotIn("hostPath", str(pod))
        self.assertNotIn("persistentVolumeClaim", str(pod))
        self.assertFalse(any("secret" in volume for volume in pod.get("volumes", [])))

    def test_runner_records_failed_status_even_when_a_check_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = root / "kubectl"
            status = root / "status.json"
            command.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'get nodes'*) exit 0 ;;\n"
                "  *'get configmap portal-pvc-backup-evidence'*) printf '%s\\n' 'source_runtime=k3s-pvc' 'backup_completed_at=2020-01-01T00:00:00Z' ;;\n"
                "  *'get deployment portal-web'*) printf 1 ;;\n"
                "  *'get pods'*) printf recovery-pod ;;\n"
                "  *'get pod recovery-pod'*) n=$(cat \"$COUNTER\" 2>/dev/null || printf 0); n=$((n + 1)); printf '%s' \"$n\" > \"$COUNTER\"; printf '%s' \"$((n - 1))\" ;;\n"
                "  *'patch configmap sre-telegram-quarterly-audit-status'*) printf '%s' \"$*\" > \"$STATUS\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            command.chmod(0o755)
            result = subprocess.run(
                ["bash", str(RUNNER)],
                env={
                    **os.environ,
                    "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
                    "STATUS": str(status),
                    "COUNTER": str(root / "counter"),
                    "KUBERNETES_SERVICE_HOST": "kubernetes.default.svc",
                    "KUBERNETES_SERVICE_PORT_HTTPS": "443",
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('"status":"failed"', status.read_text(encoding="utf-8"))
            self.assertIn('"health_check":"failed"', status.read_text(encoding="utf-8"))

    def test_runner_and_image_do_not_depend_on_host_or_sensitive_storage(self):
        text = "\n".join((RUNNER.read_text(encoding="utf-8"), DOCKERFILE.read_text(encoding="utf-8"))).lower()
        for forbidden in ("sudo", "docker", "hostpath", "privileged", "persistentvolumeclaim"):
            self.assertNotIn(forbidden, text)
        self.assertRegex(DOCKERFILE.read_text(encoding="utf-8"), re.compile(r"^FROM .+@sha256:[0-9a-f]{64}$", re.MULTILINE))
        self.assertIn("kubernetes-client", DOCKERFILE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
