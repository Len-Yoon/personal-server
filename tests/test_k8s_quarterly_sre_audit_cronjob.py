import re
import os
import json
import subprocess
import sys
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


def recovery_deployment():
    return find("Deployment", "sre-pod-recovery", "sre-recovery-lab")


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
                {"apiGroups": ["apps"], "resources": ["deployments"], "resourceNames": ["sre-pod-recovery"], "verbs": ["get", "watch"]},
                {"apiGroups": ["apps"], "resources": ["deployments/scale"], "resourceNames": ["sre-pod-recovery"], "verbs": ["get", "patch"]},
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
                {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create"]},
                {"apiGroups": [""], "resources": ["events"], "verbs": ["list"]},
            ],
        )
        for rule in lab_rules:
            self.assertNotIn("secrets", rule["resources"])
            self.assertNotIn("persistentvolumeclaims", rule["resources"])

        node_rules = find("ClusterRole", "quarterly-sre-audit-nodes")["rules"]
        self.assertEqual(node_rules, [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list"]}])

    def test_manifest_creates_only_the_fixed_recovery_namespace_and_no_sensitive_mounts(self):
        namespace = find("Namespace", "sre-recovery-lab")
        self.assertEqual(namespace["metadata"]["name"], "sre-recovery-lab")
        self.assertEqual(
            namespace["metadata"]["labels"],
            {
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": "latest",
                "pod-security.kubernetes.io/audit": "restricted",
                "pod-security.kubernetes.io/audit-version": "latest",
                "pod-security.kubernetes.io/warn": "restricted",
                "pod-security.kubernetes.io/warn-version": "latest",
            },
        )
        pod = pod_spec()
        self.assertNotIn("hostPath", str(pod))
        self.assertNotIn("persistentVolumeClaim", str(pod))
        self.assertFalse(any("secret" in volume for volume in pod.get("volumes", [])))

        recovery = recovery_deployment()
        self.assertEqual(recovery["spec"]["replicas"], 0)
        recovery_pod = recovery["spec"]["template"]["spec"]
        recovery_container = recovery_pod["containers"][0]
        self.assertFalse(recovery_pod["automountServiceAccountToken"])
        self.assertTrue(recovery_pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(recovery_pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        self.assertTrue(recovery_container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(recovery_container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(recovery_container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertIn("livenessProbe", recovery_container)

    def run_runner(self, *, evidence, scenario="", patch_fails=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = root / "kubectl"
            status = root / "status.json"
            calls = root / "calls"
            date = root / "date"
            command.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get nodes'*) printf 'node Ready worker' ;;\n"
                "  *'get configmap portal-pvc-backup-evidence'*) [ \"$SCENARIO\" = configmap-fail ] && exit 1; printf '%s\\n' \"$EVIDENCE\"; exit 0 ;;\n"
                "  *'get deployment portal-web'*) printf 1 ;;\n"
                "  *'scale deployment sre-pod-recovery --replicas=1'*) [ \"$SCENARIO\" = scale-up-fail ] && exit 1; printf 1 > \"$REPLICAS\"; exit 0 ;;\n"
                "  *'scale deployment sre-pod-recovery --replicas=0'*) [ \"$SCENARIO\" = cleanup-fail ] && exit 1; printf 0 > \"$REPLICAS\"; exit 0 ;;\n"
                "  *'get deployment sre-pod-recovery'*) cat \"$REPLICAS\" 2>/dev/null || printf 0 ;;\n"
                "  *'get pods'*) replicas=$(cat \"$REPLICAS\" 2>/dev/null || printf 0); [ \"$replicas\" = 0 ] && exit 0; printf recovery-pod ;;\n"
                "  *'get pod recovery-pod'*) n=$(cat \"$COUNTER\" 2>/dev/null || printf 0); n=$((n + 1)); printf '%s' \"$n\" > \"$COUNTER\"; printf '%s' \"$((n - 1))\" ;;\n"
                "  *'exec recovery-pod'*) [ \"$SCENARIO\" = exec-fail ] && exit 1; exit 0 ;;\n"
                "  *'patch configmap sre-telegram-quarterly-audit-status'*) printf '%s' \"$9\" > \"$STATUS\"; [ \"$PATCH_FAILS\" = true ] && exit 1; exit 0 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            date.write_text(
                "#!/bin/sh\ncase \"$*\" in *'-d'*) printf 1000 ;; *'+%s'*) printf 1001 ;; *'+%Y%m%dT%H%M%SZ'*) printf 20260912T010203Z ;; *) printf 2026-09-12T01:02:03Z ;; esac\n",
                encoding="utf-8",
            )
            command.chmod(0o755)
            date.chmod(0o755)
            result = subprocess.run(
                ["bash", str(RUNNER)],
                env={
                    **os.environ,
                    "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
                    "CALLS": str(calls),
                    "REPLICAS": str(root / "replicas"),
                    "STATUS": str(status),
                    "COUNTER": str(root / "counter"),
                    "EVIDENCE": evidence,
                    "SCENARIO": scenario,
                    "PATCH_FAILS": str(patch_fails).lower(),
                    "KUBERNETES_SERVICE_HOST": "kubernetes.default.svc",
                    "KUBERNETES_SERVICE_PORT_HTTPS": "443",
                },
                text=True,
                capture_output=True,
                check=False,
            )
            return (
                result,
                json.loads(status.read_text(encoding="utf-8")),
                calls.read_text(encoding="utf-8"),
            )

    def test_runner_emits_relay_compatible_payload_after_a_successful_run(self):
        result, payload, calls = self.run_runner(
            evidence="source_runtime=k3s-pvc\nbackup_completed_at=2026-09-12T00:00:00Z"
        )

        self.assertEqual(result.returncode, 0, f"{result.stderr}\n{calls}")
        self.assertEqual(set(payload["data"]), {"run_id", "status", "completed_at", "health_audit", "backup_check", "recovery_lab", "health_check", "backup_evidence"})
        stale_configmap = {
            "data": {
                "health_check": "passed",
                "backup_evidence": "passed",
            }
        }
        for key, value in payload["data"].items():
            if value is None:
                stale_configmap["data"].pop(key, None)
            else:
                stale_configmap["data"][key] = value
        sys.path.insert(0, str(ROOT / "sre-telegram-relay"))
        from app.main import _read_quarterly_audit_report

        class RelayClient:
            def get_config_map(self, namespace, name):
                return stale_configmap

        self.assertEqual(_read_quarterly_audit_report(RelayClient()), stale_configmap["data"])
        self.assertIn("scale deployment sre-pod-recovery --replicas=1", calls)
        self.assertIn("scale deployment sre-pod-recovery --replicas=0", calls)
        self.assertNotIn(" create ", calls)
        self.assertNotIn(" delete ", calls)

    def test_runner_fails_closed_for_wrong_runtime_or_unreadable_backup_evidence(self):
        for evidence, scenario in (
            ("source_runtime=compose-local\nbackup_completed_at=2026-09-12T00:00:00Z", ""),
            ("source_runtime=k3s-pvc\nbackup_completed_at=2026-09-12T00:00:00Z", "configmap-fail"),
        ):
            with self.subTest(scenario=scenario or "wrong-runtime"):
                result, payload, _ = self.run_runner(evidence=evidence, scenario=scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(payload["data"]["status"], "failed")
                self.assertEqual(payload["data"]["backup_check"], "failed")

    def test_runner_never_executes_when_recovery_scale_up_fails_and_restores_zero(self):
        result, payload, calls = self.run_runner(
            evidence="source_runtime=k3s-pvc\nbackup_completed_at=2026-09-12T00:00:00Z",
            scenario="scale-up-fail",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(payload["data"]["recovery_lab"], "failed")
        self.assertNotIn("exec recovery-pod", calls)
        self.assertIn("scale deployment sre-pod-recovery --replicas=0", calls)

    def test_runner_fails_when_owned_recovery_cleanup_or_status_patch_fails(self):
        evidence = "source_runtime=k3s-pvc\nbackup_completed_at=2026-09-12T00:00:00Z"
        for scenario, patch_fails in (("exec-fail", False), ("cleanup-fail", False), ("", True)):
            with self.subTest(scenario=scenario or "patch-fail"):
                result, payload, calls = self.run_runner(
                    evidence=evidence, scenario=scenario, patch_fails=patch_fails
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls.count("patch configmap sre-telegram-quarterly-audit-status"), 1)
                if scenario in {"exec-fail", "cleanup-fail"}:
                    self.assertEqual(payload["data"]["recovery_lab"], "failed")

    def test_runner_and_image_do_not_depend_on_host_or_sensitive_storage(self):
        text = "\n".join((RUNNER.read_text(encoding="utf-8"), DOCKERFILE.read_text(encoding="utf-8"))).lower()
        for forbidden in ("sudo", "docker", "hostpath", "privileged", "persistentvolumeclaim"):
            self.assertNotIn(forbidden, text)
        self.assertRegex(DOCKERFILE.read_text(encoding="utf-8"), re.compile(r"^FROM .+@sha256:[0-9a-f]{64}$", re.MULTILINE))
        self.assertIn("kubernetes-client", DOCKERFILE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
