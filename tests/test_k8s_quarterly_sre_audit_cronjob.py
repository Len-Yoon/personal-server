import re
import os
import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "infra/k8s/sre-audit-automation/quarterly-sre-audit-cronjob.yaml"
DOCKERFILE = ROOT / "infra/k8s/sre-audit-automation/Dockerfile"
RUNNER = ROOT / "infra/k8s/tools/quarterly-sre-audit-runner.sh"


def valid_backup_evidence(*, overrides=None, extra_lines=()):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "schema_version": "1",
        "scope": "portal",
        "backup_status": "success",
        "encrypted": "true",
        "backup_completed_at": (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "restore_status": "success",
        "restore_verified_at": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "evidence_expires_at": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "backup_id": "quarterly-audit-test",
        "source_runtime": "k3s-pvc",
    }
    values.update(overrides or {})
    return "\n".join([*(f"{key}={value}" for key, value in values.items()), *extra_lines])


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
        validation_status_rules = find("Role", "quarterly-sre-audit-validation-diagnostics", "monitoring")["rules"]
        self.assertEqual(
            validation_status_rules,
            [{"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["sre-quarterly-audit-diagnostics"], "verbs": ["get", "patch"]}],
        )

        official_status_binding = find("RoleBinding", "quarterly-sre-audit-status", "monitoring")
        validation_status_binding = find("RoleBinding", "quarterly-sre-audit-validation-diagnostics", "monitoring")
        self.assertEqual(official_status_binding["roleRef"]["name"], "quarterly-sre-audit-status")
        self.assertEqual(official_status_binding["subjects"], [{"kind": "ServiceAccount", "name": "quarterly-sre-audit", "namespace": "monitoring"}])
        self.assertEqual(validation_status_binding["roleRef"]["name"], "quarterly-sre-audit-validation-diagnostics")
        self.assertEqual(validation_status_binding["subjects"], [{"kind": "ServiceAccount", "name": "quarterly-sre-audit-validation", "namespace": "monitoring"}])

        lab_rules = find("Role", "quarterly-sre-audit-recovery-lab", "sre-recovery-lab")["rules"]
        self.assertEqual(
            lab_rules,
            [
                {"apiGroups": ["apps"], "resources": ["deployments"], "resourceNames": ["sre-pod-recovery"], "verbs": ["get"]},
                {"apiGroups": ["apps"], "resources": ["deployments/scale"], "resourceNames": ["sre-pod-recovery"], "verbs": ["get", "patch"]},
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
                {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["sre-pod-recovery-trigger"], "verbs": ["get", "patch"]},
                {"apiGroups": [""], "resources": ["events"], "verbs": ["list"]},
            ],
        )
        self.assertEqual(
            find("ConfigMap", "sre-pod-recovery-trigger", "sre-recovery-lab")["data"],
            {"trigger": "false"},
        )
        self.assertIn(
            {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["sre-pod-recovery-trigger"], "verbs": ["get", "patch"]},
            lab_rules,
        )
        self.assertNotIn("pods/exec", str(lab_rules))
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

    def test_fixed_recovery_emptydir_is_writable_for_the_initial_health_marker_only(self):
        """The fixed non-PVC recovery Pod must create its first /tmp health marker as UID 10001."""
        recovery_pod = recovery_deployment()["spec"]["template"]["spec"]
        recovery_container = recovery_pod["containers"][0]

        self.assertEqual(recovery_pod["securityContext"].get("fsGroup"), 10001)
        self.assertIn(
            {"name": "recovery-tmp", "emptyDir": {"sizeLimit": "16Mi"}},
            recovery_pod["volumes"],
        )
        self.assertIn({"name": "recovery-tmp", "mountPath": "/tmp"}, recovery_container["volumeMounts"])
        self.assertNotIn("fsGroup", pod_spec()["securityContext"])
        self.assertNotIn("persistentVolumeClaim", str(recovery_pod))
        self.assertFalse(any("secret" in volume for volume in recovery_pod["volumes"]))

    def test_recovery_trigger_is_a_readonly_directory_projection(self):
        recovery_pod = recovery_deployment()["spec"]["template"]["spec"]
        mounts = recovery_pod["containers"][0]["volumeMounts"]
        trigger_mounts = [mount for mount in mounts if mount["mountPath"] == "/var/run/recovery-trigger"]
        self.assertEqual(len(trigger_mounts), 1)
        mount = trigger_mounts[0]
        self.assertTrue(mount.get("readOnly"))
        self.assertNotIn("subPath", mount)
        self.assertNotIn("subPathExpr", mount)
        volume = next(volume for volume in recovery_pod["volumes"] if volume["name"] == mount["name"])
        self.assertEqual(volume["configMap"]["name"], "sre-pod-recovery-trigger")

    def test_recovery_command_injects_once_and_restores_health_on_container_restart(self):
        command = recovery_deployment()["spec"]["template"]["spec"]["containers"][0]["command"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trigger_dir = root / "trigger-volume"
            trigger_dir.mkdir()
            trigger = trigger_dir / "trigger"
            trigger.write_text("false", encoding="utf-8")
            script = command[2].replace("/var/run/recovery-trigger", str(trigger_dir)).replace("/tmp/", f"{root}/")
            script = script.replace("sleep 1", "sleep 0.01")
            healthy = root / "healthy"
            marker = root / "recovery-fault-injected"

            def wait_for(predicate):
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(0.01)
                self.fail("recovery command did not reach the expected health state")

            for restart in (False, True):
                process = subprocess.Popen(command[:2] + [script], start_new_session=True)
                try:
                    wait_for(healthy.exists)
                    if not restart:
                        self.assertFalse(marker.exists())
                        trigger.write_text("true", encoding="utf-8")
                        wait_for(lambda: not healthy.exists())
                        self.assertTrue(marker.exists())
                    else:
                        time.sleep(0.1)
                        self.assertTrue(healthy.exists())
                        self.assertTrue(marker.exists())
                finally:
                    os.killpg(process.pid, 15)
                    process.wait(timeout=2)

    def run_runner(self, *, evidence, scenario="", patch_fails=False, report_mode="official"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = root / "kubectl"
            status = root / "status.json"
            calls = root / "calls"
            date = root / "date"
            diagnostics = root / "diagnostics.json"
            command.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get nodes'*) printf 'node Ready worker' ;;\n"
                "  *'get configmap portal-pvc-backup-evidence'*) [ \"$SCENARIO\" = configmap-fail ] && exit 1; printf '%s\\n' \"$EVIDENCE\"; exit 0 ;;\n"
                "  *'get deployment portal-web'*) printf 1 ;;\n"
                "  *'scale deployment sre-pod-recovery --replicas=1'*) [ \"$SCENARIO\" = scale-up-fail ] && exit 1; printf 1 > \"$REPLICAS\"; exit 0 ;;\n"
                "  *'scale deployment sre-pod-recovery --replicas=0'*) [ \"$SCENARIO\" = cleanup-fail ] && exit 1; printf 0 > \"$REPLICAS\"; exit 0 ;;\n"
                "  *'get deployment sre-pod-recovery'*'.spec.replicas'*) case \"$SCENARIO\" in cleanup-get-fail) exit 1 ;; cleanup-nonzero) printf 1; exit 0 ;; cleanup-empty) exit 0 ;; cleanup-malformed) printf unknown; exit 0 ;; esac; cat \"$REPLICAS\" 2>/dev/null || printf 0 ;;\n"
                "  *'get deployment sre-pod-recovery'*) replicas=$(cat \"$REPLICAS\" 2>/dev/null || printf 0); [ \"$replicas\" = 1 ] && printf True:1 || printf False:0 ;;\n"
                "  *'get pods'*) replicas=$(cat \"$REPLICAS\" 2>/dev/null || printf 0); if [ \"$replicas\" = 0 ] && [ \"$SCENARIO\" != cleanup-terminating ]; then exit 0; fi; [ \"$SCENARIO\" = terminating-old ] && { printf 'recovery-pod-old\\nrecovery-pod\\n'; exit 0; }; [ \"$SCENARIO\" = multiple-pods ] && [ -f \"$TRIGGERED\" ] && { printf 'recovery-pod\\nrecovery-pod-2\\n'; exit 0; }; printf 'recovery-pod\\n' ;;\n"
                "  *'wait --for=condition=Ready pod/recovery-pod --timeout=60s'*) [ \"$SCENARIO\" = ready-wait-fail ] && exit 1; exit 0 ;;\n"
                "  *'get events --field-selector involvedObject.name=recovery-pod'*) [ \"$SCENARIO\" = events-fail ] && exit 1; exit 0 ;;\n"
                "  *'get pod recovery-pod-old'*'containerStatuses'*) printf 'uid-old|Running|2026-09-13T00:00:00Z|False|recovery=containerd://old=0;' ;;\n"
                "  *'get pod recovery-pod'*'containerStatuses'*) if [ ! -f \"$TRIGGERED\" ]; then printf 'uid-1|Running||True|recovery=containerd://before=0;'; exit 0; fi; n=$(cat \"$COUNTER\" 2>/dev/null || printf 0); n=$((n + 1)); printf '%s' \"$n\" > \"$COUNTER\"; case \"$SCENARIO\" in restart-without-not-ready) printf 'uid-1|Running||True|recovery=containerd://after=1;' ;; stale-ready) printf 'uid-1|Running||True|recovery=containerd://before=0;' ;; pod-replacement) printf 'uid-2|Running||True|recovery=containerd://after=1;' ;; transient-empty-status) [ \"$n\" = 1 ] && { printf 'uid-1|Running||False|'; exit 0; }; [ \"$n\" = 2 ] && { printf 'uid-1|Running||False|recovery=containerd://before=0;'; exit 0; }; printf 'uid-1|Running||True|recovery=containerd://after=1;' ;; *) [ \"$n\" = 1 ] && printf 'uid-1|Running||False|recovery=containerd://before=0;' || printf 'uid-1|Running||True|recovery=containerd://after=1;' ;; esac; exit 0 ;;\n"
                "  *'get pod recovery-pod'*'Ready'*) printf True ;;\n"
                "  *'get pod recovery-pod'*) n=$(cat \"$COUNTER\" 2>/dev/null || printf 0); n=$((n + 1)); printf '%s' \"$n\" > \"$COUNTER\"; printf '%s' \"$((n - 1))\" ;;\n"
                "  *'exec recovery-pod'*) exit 1 ;;\n"
                "  *'patch configmap sre-pod-recovery-trigger'*) case \"$9\" in '{\"data\":{\"trigger\":\"true\"}}') [ \"$SCENARIO\" = trigger-patch-fail ] && exit 1; : > \"$TRIGGERED\" ;; '{\"data\":{\"trigger\":\"false\"}}') [ \"$SCENARIO\" = trigger-reset-fail ] && exit 1; [ \"$SCENARIO\" = cleanup-trigger-fail ] && [ -f \"$TRIGGERED\" ] && exit 1 ;; *) exit 1 ;; esac; exit 0 ;;\n"
                "  *'patch configmap sre-telegram-quarterly-audit-status'*) printf '%s' \"$9\" > \"$STATUS\"; [ \"$PATCH_FAILS\" = true ] && exit 1; exit 0 ;;\n"
                "  *'patch configmap sre-quarterly-audit-diagnostics'*) printf '%s' \"$9\" > \"$DIAGNOSTICS\"; [ \"$PATCH_FAILS\" = true ] && exit 1; exit 0 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            date.write_text(
                "#!/bin/sh\ncase \"$*\" in *'-d'*) printf 1000 ;; *'+%s'*) printf 1001 ;; *'+%Y%m%dT%H%M%SZ'*) printf 20260912T010203Z ;; *) printf 2026-09-12T01:02:03Z ;; esac\n",
                encoding="utf-8",
            )
            (root / "sleep").write_text("#!/bin/sh\n/bin/sleep 0.01\n", encoding="utf-8")
            command.chmod(0o755)
            date.chmod(0o755)
            (root / "sleep").chmod(0o755)
            result = subprocess.run(
                ["bash", str(RUNNER)],
                env={
                    **os.environ,
                    "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
                    "CALLS": str(calls),
                    "REPLICAS": str(root / "replicas"),
                    "STATUS": str(status),
                    "COUNTER": str(root / "counter"),
                    "TRIGGERED": str(root / "triggered"),
                    "EVIDENCE": evidence,
                    "SCENARIO": scenario,
                    "PATCH_FAILS": str(patch_fails).lower(),
                    "DIAGNOSTICS": str(diagnostics),
                    "QUARTERLY_SRE_AUDIT_REPORT_MODE": report_mode,
                    "QUARTERLY_SRE_AUDIT_DIAGNOSTICS_CONFIGMAP": "sre-quarterly-audit-diagnostics",
                    "QUARTERLY_SRE_AUDIT_RECOVERY_TIMEOUT_SECONDS": "2",
                    "QUARTERLY_SRE_AUDIT_RECOVERY_POLL_INTERVAL_SECONDS": "0.01",
                    "KUBERNETES_SERVICE_HOST": "kubernetes.default.svc",
                    "KUBERNETES_SERVICE_PORT_HTTPS": "443",
                },
                text=True,
                capture_output=True,
                check=False,
            )
            return result, (json.loads(status.read_text(encoding="utf-8")) if status.exists() else None), (json.loads(diagnostics.read_text(encoding="utf-8")) if diagnostics.exists() else None), calls.read_text(encoding="utf-8")

    def test_runner_emits_relay_compatible_payload_after_a_successful_run(self):
        result, payload, _, calls = self.run_runner(evidence=valid_backup_evidence())

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
        trigger_calls = [call for call in calls.splitlines() if "patch configmap sre-pod-recovery-trigger" in call]
        self.assertEqual(len(trigger_calls), 3)
        self.assertIn('{"data":{"trigger":"false"}}', trigger_calls[0])
        self.assertIn('{"data":{"trigger":"true"}}', trigger_calls[1])
        self.assertIn('{"data":{"trigger":"false"}}', trigger_calls[2])
        self.assertLess(calls.index(trigger_calls[0]), calls.index("scale deployment sre-pod-recovery --replicas=1"))
        self.assertLess(calls.index("get pod recovery-pod"), calls.index(trigger_calls[1]))
        self.assertLess(calls.rindex(trigger_calls[2]), calls.index("scale deployment sre-pod-recovery --replicas=0"))
        self.assertNotIn(" exec ", calls)
        self.assertNotIn(" create ", calls)
        self.assertNotIn(" delete ", calls)
        self.assertIn("get pod recovery-pod", calls)
        self.assertNotIn("wait --for=condition=Ready", calls)

    def test_runner_uses_deployment_get_polling_and_bounded_recovery_transition_observation(self):
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn("get deployment \"$RECOVERY_DEPLOYMENT\"", text)
        self.assertIn(".status.availableReplicas", text)
        self.assertIn("read_recovery_pod_snapshot", text)
        self.assertIn("RECOVERY_TIMEOUT_SECONDS", text)
        self.assertNotIn("get deployments", text)

    def test_runner_allows_ninety_seconds_for_recovery_transition_after_restart(self):
        """The transition deadline must exceed the Pod's 30-second termination grace period."""
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn("RECOVERY_TIMEOUT_SECONDS=${QUARTERLY_SRE_AUDIT_RECOVERY_TIMEOUT_SECONDS:-90}", text)
        self.assertNotIn("RECOVERY_TIMEOUT_SECONDS=${QUARTERLY_SRE_AUDIT_RECOVERY_TIMEOUT_SECONDS:-30}", text)

    def test_runner_accepts_verified_scale_down_while_pod_termination_is_asynchronous(self):
        result, payload, _, calls = self.run_runner(
            evidence=valid_backup_evidence(), scenario="cleanup-terminating"
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(payload["data"]["status"], "passed")
        self.assertEqual(payload["data"]["recovery_lab"], "passed")
        cleanup_calls = calls.split("scale deployment sre-pod-recovery --replicas=0", 1)[1]
        self.assertIn("get deployment sre-pod-recovery -o jsonpath={.spec.replicas}", cleanup_calls)
        self.assertNotIn("get pods", cleanup_calls)
        self.assertNotIn("wait ", cleanup_calls)

    def test_runner_rejects_unverified_scale_down(self):
        for scenario in ("cleanup-get-fail", "cleanup-nonzero", "cleanup-empty", "cleanup-malformed"):
            with self.subTest(scenario=scenario):
                result, payload, _, _ = self.run_runner(
                    evidence=valid_backup_evidence(), scenario=scenario
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(payload["data"]["status"], "failed")
                self.assertEqual(payload["data"]["recovery_lab"], "failed")
                self.assertIn("stage=cleanup_scale_down", result.stdout)

    def test_recovery_lab_rbac_retains_pod_watch_for_bounded_ready_wait(self):
        lab_rules = find("Role", "quarterly-sre-audit-recovery-lab", "sre-recovery-lab")["rules"]
        self.assertIn(
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
            lab_rules,
        )

    def test_runner_fails_closed_for_wrong_runtime_or_unreadable_backup_evidence(self):
        for evidence, scenario in (
            (valid_backup_evidence(overrides={"source_runtime": "compose-local"}), ""),
            (valid_backup_evidence(), "configmap-fail"),
        ):
            with self.subTest(scenario=scenario or "wrong-runtime"):
                result, payload, _, _ = self.run_runner(evidence=evidence, scenario=scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(payload["data"]["status"], "failed")
                self.assertEqual(payload["data"]["backup_check"], "failed")

    def test_runner_rejects_invalid_backup_evidence_contracts(self):
        cases = {
            "failed-restore": valid_backup_evidence(overrides={"restore_status": "failed"}),
            "unencrypted-backup": valid_backup_evidence(overrides={"encrypted": "false"}),
            "expired-evidence": valid_backup_evidence(
                overrides={"evidence_expires_at": "2000-01-01T00:00:00Z"}
            ),
            "duplicate-runtime": valid_backup_evidence(
                extra_lines=("source_runtime=compose-local",)
            ),
        }
        for name, evidence in cases.items():
            with self.subTest(name=name):
                result, payload, _, _ = self.run_runner(evidence=evidence)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(payload["data"]["status"], "failed")
                self.assertEqual(payload["data"]["backup_check"], "failed")

    def test_runner_never_executes_when_recovery_scale_up_fails_and_restores_zero(self):
        result, payload, _, calls = self.run_runner(
            evidence=valid_backup_evidence(),
            scenario="scale-up-fail",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(payload["data"]["recovery_lab"], "failed")
        self.assertNotIn("exec recovery-pod", calls)
        self.assertIn("scale deployment sre-pod-recovery --replicas=0", calls)

    def test_runner_accepts_a_completed_restart_when_not_ready_is_between_polling_intervals(self):
        """A real restart remains valid when the transient NotReady state is not sampled."""
        result, payload, _, _ = self.run_runner(
            evidence=valid_backup_evidence(),
            scenario="restart-without-not-ready",
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(payload["data"]["status"], "passed")
        self.assertEqual(payload["data"]["recovery_lab"], "passed")

    def test_runner_rejects_a_stale_ready_state_without_restart_evidence(self):
        result, payload, _, _ = self.run_runner(
            evidence=valid_backup_evidence(),
            scenario="stale-ready",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(payload["data"]["status"], "failed")
        self.assertEqual(payload["data"]["recovery_lab"], "failed")
        self.assertIn("stage=recovery_transition", result.stdout)

    def test_runner_accepts_one_live_pod_while_an_old_terminating_pod_is_present(self):
        result, payload, _, _ = self.run_runner(
            evidence=valid_backup_evidence(),
            scenario="terminating-old",
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(payload["data"]["recovery_lab"], "passed")

    def test_runner_retries_an_empty_container_status_during_the_observed_transition(self):
        result, payload, _, _ = self.run_runner(
            evidence=valid_backup_evidence(),
            scenario="transient-empty-status",
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(payload["data"]["recovery_lab"], "passed")

    def test_runner_fails_closed_when_the_recovery_pod_is_replaced_or_duplicated(self):
        for scenario in ("pod-replacement", "multiple-pods"):
            with self.subTest(scenario=scenario):
                result, payload, _, _ = self.run_runner(
                    evidence=valid_backup_evidence(),
                    scenario=scenario,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(payload["data"]["recovery_lab"], "failed")
                self.assertIn("stage=recovery_transition", result.stdout)

    def test_validation_mode_reports_only_allowlisted_diagnostics_without_touching_relay_status(self):
        result, payload, diagnostics, calls = self.run_runner(
            evidence=valid_backup_evidence(),
            report_mode="validation",
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIsNone(payload)
        self.assertEqual(
            set(diagnostics["data"]),
            {"run_id", "result", "failure_stage", "cleanup_status", "completed_at"},
        )
        self.assertEqual(diagnostics["data"]["result"], "passed")
        self.assertEqual(diagnostics["data"]["failure_stage"], "none")
        self.assertEqual(diagnostics["data"]["cleanup_status"], "passed")
        self.assertIn("patch configmap sre-quarterly-audit-diagnostics", calls)
        self.assertNotIn("patch configmap sre-telegram-quarterly-audit-status", calls)

    def test_validation_mode_fails_closed_when_diagnostics_patch_fails(self):
        result, payload, diagnostics, calls = self.run_runner(
            evidence=valid_backup_evidence(),
            report_mode="validation",
            patch_fails=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(payload)
        self.assertEqual(diagnostics["data"]["result"], "passed")
        self.assertIn("patch configmap sre-quarterly-audit-diagnostics", calls)
        self.assertNotIn("patch configmap sre-telegram-quarterly-audit-status", calls)

    def test_runner_logs_non_sensitive_stage_when_recovery_events_fail(self):
        for scenario, stage in (("events-fail", "events"),):
            with self.subTest(scenario=scenario):
                result, payload, _, _ = self.run_runner(
                    evidence=valid_backup_evidence(),
                    scenario=scenario,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(payload["data"]["status"], "failed")
                self.assertEqual(payload["data"]["recovery_lab"], "failed")
                self.assertIn(
                    f"quarterly_sre_audit_check=recovery_lab result=failed stage={stage}",
                    result.stdout,
                )
                self.assertNotIn("source_runtime=k3s-pvc", result.stdout)
                self.assertNotIn("recovery-pod", result.stdout)

    def test_runner_fails_when_owned_recovery_cleanup_or_status_patch_fails(self):
        evidence = valid_backup_evidence()
        for scenario, patch_fails in (("trigger-patch-fail", False), ("cleanup-fail", False), ("", True)):
            with self.subTest(scenario=scenario or "patch-fail"):
                result, payload, _, calls = self.run_runner(
                    evidence=evidence, scenario=scenario, patch_fails=patch_fails
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls.count("patch configmap sre-telegram-quarterly-audit-status"), 1)
                if scenario in {"trigger-patch-fail", "cleanup-fail"}:
                    self.assertEqual(payload["data"]["recovery_lab"], "failed")

    def test_runner_fails_closed_on_trigger_patch_or_reset_failure_and_still_scales_down(self):
        for scenario, stage in (
            ("trigger-patch-fail", "trigger_activate"),
            ("trigger-reset-fail", "cleanup_trigger_reset"),
            ("cleanup-trigger-fail", "cleanup_trigger_reset"),
        ):
            with self.subTest(scenario=scenario):
                result, payload, diagnostics, calls = self.run_runner(
                    evidence=valid_backup_evidence(), scenario=scenario, report_mode="validation"
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(payload)
                self.assertEqual(diagnostics["data"]["result"], "failed")
                self.assertIn(stage, diagnostics["data"]["failure_stage"])
                self.assertEqual(diagnostics["data"]["cleanup_status"], "passed" if scenario == "trigger-patch-fail" else "failed")
                self.assertIn("scale deployment sre-pod-recovery --replicas=0", calls)
                reset_call = [call for call in calls.splitlines() if '"trigger":"false"' in call][-1]
                self.assertLess(calls.rindex(reset_call), calls.index("scale deployment sre-pod-recovery --replicas=0"))
                self.assertNotIn(" exec ", calls)
                if scenario == "trigger-reset-fail":
                    self.assertNotIn("--replicas=1", calls)

    def test_runner_logs_non_sensitive_stage_when_recovery_cleanup_fails(self):
        result, payload, _, _ = self.run_runner(
            evidence=valid_backup_evidence(),
            scenario="cleanup-fail",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(payload["data"]["status"], "failed")
        self.assertEqual(payload["data"]["recovery_lab"], "failed")
        self.assertIn(
            "quarterly_sre_audit_check=recovery_lab result=failed stage=cleanup_scale_down",
            result.stdout,
        )
        self.assertNotIn("source_runtime=k3s-pvc", result.stdout)
        self.assertNotIn("recovery-pod", result.stdout)

    def test_runner_and_image_do_not_depend_on_host_or_sensitive_storage(self):
        text = "\n".join((RUNNER.read_text(encoding="utf-8"), DOCKERFILE.read_text(encoding="utf-8"))).lower()
        for forbidden in ("sudo", "docker", "hostpath", "privileged", "persistentvolumeclaim"):
            self.assertNotIn(forbidden, text)
        self.assertRegex(DOCKERFILE.read_text(encoding="utf-8"), re.compile(r"^FROM .+@sha256:[0-9a-f]{64}$", re.MULTILINE))
        self.assertIn("kubernetes-client", DOCKERFILE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
