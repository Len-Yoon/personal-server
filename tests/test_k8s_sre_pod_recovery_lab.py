import pathlib
import os
import subprocess
import tempfile
import unittest

import yaml


SCRIPT = pathlib.Path(__file__).parents[1] / "infra/k8s/tools/sre-pod-recovery-lab.sh"


class K3sSrePodRecoveryLabTest(unittest.TestCase):
    def test_readme_documents_operator_only_recovery_lab(self):
        readme = pathlib.Path(__file__).parents[1] / "infra/k8s/README.md"
        text = readme.read_text(encoding="utf-8")
        self.assertIn("sre-pod-recovery-lab.sh --run", text)
        self.assertIn("sre-pod-recovery-lab.sh --cleanup", text)
        self.assertIn("sre-recovery-lab-<run-id>", text)
        self.assertIn("실습 리소스는 sre-recovery-lab-<run-id> namespace에만 생성된다.", text)
        self.assertIn("`restartCount`가 증가한", text)
        self.assertIn("Pod가 `Ready` 조건으로 복구됨", text)
        self.assertIn("Portal", text)
        self.assertIn("Compose", text)
        self.assertIn("Caddy", text)
        self.assertIn("scheduler", text)
        self.assertIn("변경하지 않는다", text)
        self.assertIn("이 실습은 다른 namespace, Portal, Compose, Caddy, scheduler를 변경하거나 재시작하지 않는다.", text)
        self.assertNotIn("새 Pod", text)

    def test_lab_uses_isolated_deployment_and_liveness_sentinel(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for required in (
            'NS="sre-recovery-lab-${run_id_lc}"',
            "kind: Namespace",
            "kind: Deployment",
            "image: busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662",
            "automountServiceAccountToken: false",
            "runAsNonRoot: true",
            "allowPrivilegeEscalation: false",
            "drop: [\"ALL\"]",
            "mountPath: /tmp",
            "emptyDir: {}",
            "livenessProbe:",
            "test ! -f /tmp/force-liveness-failure",
            "touch /tmp/force-liveness-failure",
            "restartCount",
            "--for=condition=Ready",
        ):
            self.assertIn(required, text)
        self.assertNotIn("portal-web", text)
        self.assertNotIn("docker compose", text.lower())

    def test_lab_deployment_manifest_enforces_hardened_writable_tmp_only_contract(self):
        text = SCRIPT.read_text(encoding="utf-8")
        manifest = text.split("sudo k3s kubectl apply -f - <<EOF\n", 1)[1].split("\nEOF", 1)[0]
        deployment = yaml.safe_load(manifest)
        pod_spec = deployment["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]
        security = container["securityContext"]

        self.assertRegex(container["image"], r"^busybox@sha256:[0-9a-f]{64}$")
        self.assertFalse(pod_spec["automountServiceAccountToken"])
        self.assertEqual(pod_spec["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        self.assertTrue(pod_spec["securityContext"]["runAsNonRoot"])
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertEqual(security["capabilities"]["drop"], ["ALL"])
        self.assertTrue(security["readOnlyRootFilesystem"])
        self.assertEqual(container["volumeMounts"], [{"name": "tmp", "mountPath": "/tmp"}])
        self.assertEqual(pod_spec["volumes"], [{"name": "tmp", "emptyDir": {}}])

    def test_apply_failure_deletes_only_the_current_lab_namespace(self):
        with tempfile.TemporaryDirectory() as td:
            calls = pathlib.Path(td) / "calls"
            fake = pathlib.Path(td) / "sudo"
            fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALLS\"\n[ \"$3\" = get ] && { echo \"Error from server (NotFound): namespaces \\\"sre-recovery-lab-apply-failure\\\" not found\" >&2; exit 1; }\n[ \"$3\" = apply ] && exit 42\nexit 0\n")
            fake.chmod(0o755)
            env = {**os.environ, "PATH": f"{td}:{os.environ['PATH']}", "CALLS": str(calls), "SRE_RECOVERY_LAB_RUN_ID": "apply-failure"}
            result = subprocess.run(["bash", str(SCRIPT), "--run"], env=env, check=False, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            recorded = calls.read_text()
            self.assertIn("delete namespace sre-recovery-lab-apply-failure", recorded)
            self.assertNotIn("portal-web", recorded)

    def test_namespace_api_error_fails_without_apply_or_delete(self):
        with tempfile.TemporaryDirectory() as td:
            calls = pathlib.Path(td) / "calls"
            fake = pathlib.Path(td) / "sudo"
            fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALLS\"\n[ \"$3\" = create ] && { echo 'connection refused' >&2; exit 1; }\nexit 0\n")
            fake.chmod(0o755)
            env = {**os.environ, "PATH": f"{td}:{os.environ['PATH']}", "CALLS": str(calls), "SRE_RECOVERY_LAB_RUN_ID": "api-error"}
            result = subprocess.run(["bash", str(SCRIPT), "--run"], env=env, check=False, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            recorded = calls.read_text()
            self.assertNotIn(" apply ", recorded)
            self.assertNotIn(" delete ", recorded)

    def test_namespace_auth_and_generic_notfound_errors_are_not_absence(self):
        for message, run_id in (("Error from server (Forbidden): authentication required", "auth-error"), ("Error from server (NotFound): the server could not find the requested resource", "generic-notfound")):
            with self.subTest(run_id=run_id), tempfile.TemporaryDirectory() as td:
                calls = pathlib.Path(td) / "calls"
                fake = pathlib.Path(td) / "sudo"
                fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALLS\"\n[ \"$3\" = create ] && {{ echo '{message}' >&2; exit 1; }}\nexit 0\n")
                fake.chmod(0o755)
                env = {**os.environ, "PATH": f"{td}:{os.environ['PATH']}", "CALLS": str(calls), "SRE_RECOVERY_LAB_RUN_ID": run_id}
                result = subprocess.run(["bash", str(SCRIPT), "--run"], env=env, check=False, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                recorded = calls.read_text()
                self.assertNotIn(" apply ", recorded)
                self.assertNotIn(" delete ", recorded)

    def test_already_existing_namespace_is_not_owned_or_applied(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('create namespace "$NS"', text)
        self.assertIn("AlreadyExists", text)

    def test_uppercase_run_id_is_rejected_without_kubectl(self):
        with tempfile.TemporaryDirectory() as td:
            fake = pathlib.Path(td) / "sudo"
            calls = pathlib.Path(td) / "calls"
            fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALLS\"\n")
            fake.chmod(0o755)
            env = {**os.environ, "PATH": f"{td}:{os.environ['PATH']}", "CALLS": str(calls)}
            result = subprocess.run(["bash", str(SCRIPT), "--cleanup", "Lab-A"], env=env, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(calls.exists())

    def test_success_evidence_keys_and_event_query_are_present(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for key in ("pod=", "restarts_before=", "restarts_after=", "ready=true", "event_seen="):
            self.assertIn(key, text)
        self.assertIn("get events", text)


if __name__ == "__main__":
    unittest.main()
