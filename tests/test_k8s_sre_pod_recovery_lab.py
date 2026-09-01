import pathlib
import os
import subprocess
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "infra/k8s/tools/sre-pod-recovery-lab.sh"


class K3sSrePodRecoveryLabTest(unittest.TestCase):
    def test_lab_uses_isolated_deployment_and_liveness_sentinel(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for required in (
            'NS="sre-recovery-lab-${run_id_lc}"',
            "kind: Namespace",
            "kind: Deployment",
            "image: busybox:1.36",
            "livenessProbe:",
            "test ! -f /tmp/force-liveness-failure",
            "touch /tmp/force-liveness-failure",
            "restartCount",
            "--for=condition=Ready",
        ):
            self.assertIn(required, text)
        self.assertNotIn("portal-web", text)
        self.assertNotIn("docker compose", text.lower())

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
            fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALLS\"\n[ \"$3\" = get ] && { echo 'connection refused' >&2; exit 1; }\nexit 0\n")
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
                fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALLS\"\n[ \"$3\" = get ] && {{ echo '{message}' >&2; exit 1; }}\nexit 0\n")
                fake.chmod(0o755)
                env = {**os.environ, "PATH": f"{td}:{os.environ['PATH']}", "CALLS": str(calls), "SRE_RECOVERY_LAB_RUN_ID": run_id}
                result = subprocess.run(["bash", str(SCRIPT), "--run"], env=env, check=False, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                recorded = calls.read_text()
                self.assertNotIn(" apply ", recorded)
                self.assertNotIn(" delete ", recorded)


if __name__ == "__main__":
    unittest.main()
