import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/k8s/tools/portal-pvc-backup-verify.sh"


class PortalPvcBackupVerifyTests(unittest.TestCase):
    def run_tool(self, mode="--go", *, runtime="k3s", fail_at="", missing_pvc=False, repeat=False, second_runtime=None, namespace=None, existing_evidence="", special_entry=False, send_signal=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            files = root / "pvc-files"
            state = root / "pvc-state"
            remote = root / "remote"
            evidence = root / "evidence" / "portal.evidence"
            calls = root / "calls.log"
            manifest = root / "reader-manifest.yaml"
            for path in (bin_dir, files, state, remote):
                path.mkdir(parents=True)
            evidence.parent.mkdir(parents=True)
            (files / "note.txt").write_text("same\n", encoding="utf-8")
            (state / "homeops.sqlite3").write_text("fake sqlite\n", encoding="utf-8")
            (root / "recipient.txt").write_text("recipient\n", encoding="utf-8")
            (root / "identity.txt").write_text("identity\n", encoding="utf-8")
            marker = root / "runtime.mode"
            marker.write_text(runtime + "\n", encoding="utf-8")
            if existing_evidence:
                evidence.write_text(existing_evidence, encoding="utf-8")
            self.write_fakes(bin_dir, root, calls, manifest, files, state, remote)
            env = {
                **os.environ,
                "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                "PORTAL_NAMESPACE": "personal-server",
                "PORTAL_RUNTIME_MARKER": str(marker),
                "PORTAL_BACKUP_EVIDENCE": str(evidence),
                "PORTAL_AGE_RECIPIENT": str(root / "recipient.txt"),
                "PORTAL_AGE_IDENTITY": str(root / "identity.txt"),
                "PORTAL_BACKUP_REMOTE": f"fake:{remote}",
                "PORTAL_FAKE_FAIL_AT": fail_at,
                "PORTAL_FAKE_MISSING_PVC": "1" if missing_pvc else "",
                "PORTAL_FAKE_SPECIAL_ENTRY": "1" if special_entry else "",
                "PORTAL_FAKE_HOLD": "1" if send_signal else "",
                "PORTAL_FAKE_READER_WAIT": str(root / "reader-wait"),
                "PORTAL_FAKE_NOISY": "1",
            }
            if namespace is not None:
                env["PORTAL_NAMESPACE"] = namespace
            if send_signal:
                process = subprocess.Popen(["bash", str(SCRIPT), mode], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                reader_wait = root / "reader-wait"
                deadline = time.time() + 5
                while not reader_wait.exists() and time.time() < deadline:
                    time.sleep(0.01)
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=10)
                result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
            else:
                result = subprocess.run(["bash", str(SCRIPT), mode], env=env, capture_output=True, text=True)
            if repeat:
                if second_runtime is not None and evidence.exists():
                    evidence.write_text(
                        evidence.read_text(encoding="utf-8").replace(
                            "source_runtime=k3s-pvc", f"source_runtime={second_runtime}"
                        ),
                        encoding="utf-8",
                    )
                result = subprocess.run(["bash", str(SCRIPT), mode], env=env, capture_output=True, text=True)
            recorded_calls = calls.read_text(encoding="utf-8") if calls.exists() else ""
            recorded_manifest = manifest.read_text(encoding="utf-8") if manifest.exists() else ""
            recorded_evidence = evidence.read_text(encoding="utf-8") if evidence.exists() else ""
            return result, recorded_calls, recorded_manifest, recorded_evidence

    @staticmethod
    def write_fakes(bin_dir, root, calls, manifest, files, state, remote):
        def write(name, text):
            path = bin_dir / name
            path.write_text(text, encoding="utf-8")
            path.chmod(0o755)

        write("sudo", "#!/bin/sh\nexec \"$@\"\n")
        write("timeout", "#!/bin/sh\nshift\nexec \"$@\"\n")
        write(
            "k3s",
            f'''#!/bin/sh
set -eu
printf '%s\\n' "k3s $*" >> '{calls}'
shift
if [ "${{PORTAL_FAKE_FAIL_AT:-}}" = reader ]; then
  case "$*" in *'wait --for=condition=Ready'*) exit 42 ;; esac
fi
if [ "${{PORTAL_FAKE_FAIL_AT:-}}" = rollout ]; then
  case "$*" in *'rollout status deployment/portal-web'*) exit 42 ;; esac
fi
if [ "${{PORTAL_FAKE_FAIL_AT:-}}" = health ]; then
  case "$*" in *'exec portal-web-1'*) exit 42 ;; esac
fi
if [ "${{PORTAL_FAKE_FAIL_AT:-}}" = stream ]; then
  case "$*" in *'exec -i'*) exit 42 ;; esac
fi
if [ "${{PORTAL_FAKE_FAIL_AT:-}}" = scale ]; then
  case "$*" in *'scale deployment/portal-web --replicas=0'*) exit 42 ;; esac
fi
if [ "${{PORTAL_FAKE_FAIL_AT:-}}" = restore ]; then
  case "$*" in *'scale deployment/portal-web --replicas=1'*) exit 42 ;; esac
fi
if [ "${{PORTAL_FAKE_MISSING_PVC:-}}" = 1 ]; then
  case "$*" in *'get pvc/'*) exit 42 ;; esac
fi
if [ "${{PORTAL_FAKE_FAIL_AT:-}}" = lock ]; then
  case "$*" in *'create -f -'*) exit 42 ;; esac
fi
case "$*" in
  'get nodes --no-headers') printf '%s\\n' 'node-1 Ready'; exit 0 ;;
  *'get deployment portal-web -o jsonpath={{.spec.replicas}}') printf '%s\\n' '1'; exit 0 ;;
  *'get pvc/portal-web-files-dynamic -o jsonpath={{.status.phase}}') printf '%s\\n' 'Bound'; exit 0 ;;
  *'get pvc/portal-web-state-dynamic -o jsonpath={{.status.phase}}') printf '%s\\n' 'Bound'; exit 0 ;;
  *'scale deployment/portal-web --replicas=0') exit 0 ;;
  *'scale deployment/portal-web --replicas=1') exit 0 ;;
  *'wait --for=delete pod'*) exit 0 ;;
  *'wait --for=condition=Ready pod/'*)
    touch "${{PORTAL_FAKE_READER_WAIT}}"
    if [ "${{PORTAL_FAKE_HOLD:-}}" = 1 ]; then sleep 5; fi
    exit 0 ;;
  *'create -f -')
    cat > '{manifest}'
    if [ "${{PORTAL_FAKE_FAIL_AT:-}}" = lock ] && grep -Fq 'kind: Lease' '{manifest}'; then exit 42; fi
    if [ "${{PORTAL_FAKE_FAIL_AT:-}}" = create ] && grep -Fq 'kind: Pod' '{manifest}'; then exit 42; fi
    exit 0 ;;
  *'rollout status deployment/portal-web'*) exit 0 ;;
  *'get pod -l app.kubernetes.io/name=portal-web'*) printf '%s\\n' portal-web-1; exit 0 ;;
  *'exec portal-web-1'*) printf '%s\\n' '{{"status":"ok"}}'; exit 0 ;;
  *'delete pod'*) exit 0 ;;
  *'exec -i'*'/data/files'*)
    if [ "${{PORTAL_FAKE_SPECIAL_ENTRY:-}}" = 1 ]; then exit 42; fi
    tar -C '{files}' -cf - .; exit 0 ;;
  *'exec -i'*'/data/portal-web-state'*) tar -C '{state}' -cf - .; exit 0 ;;
esac
exit 0
''',
        )
        write(
            "age",
            '''#!/bin/sh
set -eu
output=''; input=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o|-i) output=$2; shift 2 ;;
    -R) shift 2 ;;
    -d) shift ;;
    *) input=$1; shift ;;
  esac
done
cp "$input" "$output"
''',
        )
        write(
            "rclone",
            f'''#!/bin/sh
set -eu
printf '%s\\n' "rclone $*" >> '{calls}'
if [ "${{PORTAL_FAKE_FAIL_AT:-}}" = remote ] && [ "$1" = lsd ]; then
  printf '%s\\n' 'fake-remote-secret /private/noisy/path' >&2
  exit 42
fi
if [ "$1" = lsd ]; then exit 0; fi
if [ "${{PORTAL_FAKE_FAIL_AT:-}}" = upload ] && [ "$1" = copyto ]; then
  printf '%s\\n' 'fake-upload-secret /private/noisy/path' >&2
  exit 42
fi
shift
while [ "$#" -gt 0 ]; do
  case "$1" in
    --log-level) shift 2 ;;
    --*) shift ;;
    *) break ;;
  esac
done
src=$1; dst=$2
case "$src" in fake:*) src='{remote}/'"${{src#fake:}}";; esac
case "$dst" in fake:*) dst='{remote}/'"${{dst#fake:}}";; esac
mkdir -p "$(dirname "$dst")"
cp "$src" "$dst"
''',
        )
        write(
            "sqlite3",
            '''#!/bin/sh
set -eu
db=$1; query=$2
case "$query" in
  *'PRAGMA quick_check'*) printf '%s\\n' ok ;;
  *'.backup '*) destination=$(printf '%s' "$query" | sed -n "s/.*backup '\\(.*\\)'/\\1/p"); cp "$db" "$destination" ;;
esac
''',
        )

    def test_check_mode_never_scales_or_creates_reader_pod(self):
        result, calls, _, _ = self.run_tool("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("portal_pvc_backup=PASS", result.stdout)
        self.assertNotIn("scale deployment/portal-web", calls)
        self.assertNotIn("create -f", calls)

    def test_check_mode_rejects_non_k3s_runtime_without_mutation(self):
        result, calls, _, _ = self.run_tool("--check", runtime="compose")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("portal_pvc_backup=FAIL", result.stdout)
        self.assertNotIn("scale deployment/portal-web", calls)
        self.assertNotIn("create -f", calls)

    def test_check_mode_rejects_missing_pvc_without_scaling(self):
        result, calls, _, _ = self.run_tool("--check", missing_pvc=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("portal_pvc_backup=FAIL", result.stdout)
        self.assertNotIn("scale deployment/portal-web", calls)

    def test_check_mode_preserves_existing_evidence_on_success_and_failure(self):
        previous = "existing-evidence\n"
        result, _, _, evidence = self.run_tool("--check", existing_evidence=previous)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(evidence, previous)
        result, _, _, evidence = self.run_tool("--check", runtime="compose", existing_evidence=previous)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(evidence, previous)

    def test_namespace_override_is_rejected_before_kubernetes_reads(self):
        result, calls, _, _ = self.run_tool("--check", namespace="other")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("get nodes", calls)

    def test_scale_failure_still_attempts_original_replica_restore(self):
        result, calls, _, _ = self.run_tool("--go", fail_at="scale")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("scale deployment/portal-web --replicas=0", calls)
        self.assertIn("scale deployment/portal-web --replicas=1", calls)

    def test_restore_failure_cannot_report_pass(self):
        result, calls, _, _ = self.run_tool("--go", fail_at="restore")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "portal_pvc_backup=FAIL")
        self.assertIn("kubectl -n personal-server delete pod", calls)

    def test_signal_forces_fail_after_cleanup(self):
        result, calls, _, _ = self.run_tool("--go", send_signal=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("portal_pvc_backup=PASS", result.stdout)
        self.assertIn("scale deployment/portal-web --replicas=1", calls)

    def test_create_failure_still_deletes_the_reserved_reader_name(self):
        result, calls, _, _ = self.run_tool("--go", fail_at="create")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("create -f -", calls)
        self.assertIn("kubectl -n personal-server delete pod", calls)

    def test_special_entry_in_staged_pvc_tree_is_rejected_before_upload(self):
        result, calls, _, _ = self.run_tool("--go", special_entry=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("rclone copyto", calls)

    def test_go_success_writes_k3s_pvc_evidence_after_restore(self):
        result, calls, _, evidence = self.run_tool("--go")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("portal_pvc_backup=PASS", result.stdout)
        self.assertIn("source_runtime=k3s-pvc", evidence)
        self.assertIn("scale deployment/portal-web --replicas=1", calls)
        self.assertIn("rollout status deployment/portal-web", calls)
        self.assertIn("exec portal-web-1", calls)

    def test_rollout_failure_removes_evidence_and_reports_fixed_failure(self):
        result, calls, _, evidence = self.run_tool("--go", fail_at="rollout")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "portal_pvc_backup_stage=portal_readiness\nportal_pvc_backup=FAIL")
        self.assertEqual(evidence, "")
        self.assertIn("scale deployment/portal-web --replicas=1", calls)

    def test_health_failure_removes_evidence_and_reports_fixed_failure(self):
        result, calls, _, evidence = self.run_tool("--go", fail_at="health")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "portal_pvc_backup_stage=portal_health\nportal_pvc_backup=FAIL")
        self.assertEqual(evidence, "")
        self.assertIn("scale deployment/portal-web --replicas=1", calls)

    def test_lock_contention_prevents_scale_and_reader_creation(self):
        result, calls, _, evidence = self.run_tool("--go", fail_at="lock")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("portal_pvc_backup_stage=lock", result.stdout)
        self.assertIn("portal_pvc_backup=FAIL", result.stdout)
        self.assertNotIn("scale deployment/portal-web", calls)
        self.assertEqual(calls.count("create -f -"), 1)
        self.assertEqual(evidence, "")

    def test_remote_preflight_failure_prevents_scale_and_redacts_noisy_error(self):
        result, calls, _, evidence = self.run_tool("--go", fail_at="remote")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("portal_pvc_backup_stage=remote_preflight", result.stdout)
        self.assertIn("portal_pvc_backup=FAIL", result.stdout)
        self.assertNotIn("scale deployment/portal-web", calls)
        self.assertNotIn("/private/", result.stdout + result.stderr)
        self.assertNotIn("fake-remote-secret", result.stdout + result.stderr)
        self.assertEqual(evidence, "")

    def test_matching_k3s_pvc_evidence_skips_upload_after_staging(self):
        result, calls, _, _ = self.run_tool("--go", repeat=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("backup_upload=SKIPPED_UNCHANGED", result.stdout)
        self.assertEqual(calls.count("rclone copyto"), 2)
        self.assertGreaterEqual(calls.count("exec -i"), 4)

    def test_non_k3s_evidence_is_not_reused(self):
        result, calls, _, _ = self.run_tool("--go", repeat=True, second_runtime="compose-local")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("SKIPPED_UNCHANGED", result.stdout)
        self.assertEqual(calls.count("rclone copyto"), 4)

    def test_go_stream_failure_restores_original_replica_and_deletes_reader(self):
        result, calls, _, evidence = self.run_tool("--go", fail_at="stream")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("scale deployment/portal-web --replicas=1", calls)
        self.assertIn("kubectl -n personal-server delete pod", calls)
        self.assertEqual(evidence, "")

    def test_go_upload_failure_restores_original_replica_and_deletes_reader(self):
        result, calls, _, evidence = self.run_tool("--go", fail_at="upload")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("scale deployment/portal-web --replicas=1", calls)
        self.assertIn("delete pod", calls)
        self.assertNotIn("fake-upload-secret", result.stdout + result.stderr)
        self.assertNotIn("/private/noisy/path", result.stdout + result.stderr)
        self.assertEqual(evidence, "")

    def test_reader_manifest_is_read_only_and_never_restarts(self):
        result, _, manifest, _ = self.run_tool("--go", fail_at="reader")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("image: busybox", manifest)
        self.assertIn("restartPolicy: Never", manifest)
        self.assertGreaterEqual(manifest.count("readOnly: true"), 2)


if __name__ == "__main__":
    unittest.main()
