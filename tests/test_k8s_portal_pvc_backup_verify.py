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
    def run_tool(self, mode="--go", *, runtime="k3s", fail_at="", missing_pvc=False, repeat=False, second_runtime=None, namespace=None, existing_evidence="", special_entry=False, send_signal=False, lock_busy=False, require_urllib=False, health_status=200, rclone_config_password=""):
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
                "PORTAL_FAKE_LOCK_BUSY": "1" if lock_busy else "",
                "PORTAL_FAKE_REQUIRE_URLLIB": "1" if require_urllib else "",
                "PORTAL_FAKE_HEALTH_STATUS": str(health_status),
                "PORTAL_FAKE_REQUIRE_CONFIG_PASSWORD": "1" if rclone_config_password else "",
                "RCLONE_CONFIG_PASS": rclone_config_password,
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
        write("flock", "#!/bin/sh\nif [ \"${PORTAL_FAKE_LOCK_BUSY:-}\" = 1 ]; then exit 1; fi\nexit 0\n")
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
    if [ "${{PORTAL_FAKE_FAIL_AT:-}}" = create ] && grep -Fq 'kind: Pod' '{manifest}'; then exit 42; fi
    exit 0 ;;
  *'rollout status deployment/portal-web'*) exit 0 ;;
  *'get pod -l app.kubernetes.io/name=portal-web'*) printf '%s\\n' portal-web-1; exit 0 ;;
  *'exec portal-web-1'*)
    case "$*" in
      *'import urllib.request; response = urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=10); raise SystemExit(0 if response.status == 200 else 1)')
        [ "${{PORTAL_FAKE_HEALTH_STATUS:-200}}" = 200 ] && exit 0 || exit 42 ;;
      *python3*) exit 42 ;;
    esac
    exit 42 ;;
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
if [ "${{PORTAL_FAKE_REQUIRE_CONFIG_PASSWORD:-}}" = 1 ]; then
  [ "${{RCLONE_CONFIG_PASS:-}}" = test-rclone-config-password ] || exit 42
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

    def test_remote_preflight_has_a_hidden_password_prompt_and_clears_its_temporary_environment(self):
        """An interactive config password must be explained without exposing it."""
        text = SCRIPT.read_text(encoding="utf-8")
        remote_preflight = text[text.index("assert_remote_access() {") : text.index("acquire_lock() {")]

        self.assertIn("read -r -s RCLONE_CONFIG_PASS", remote_preflight)
        self.assertIn("RCLONE_CONFIG_PASS_PROMPTED=1", remote_preflight)
        cleanup = text[text.index("cleanup() {") : text.index('case "${1:-}" in')]
        self.assertIn("unset RCLONE_CONFIG_PASS", cleanup)
        self.assertIn("rclone 설정 암호 입력 필요:", remote_preflight)

    def test_remote_preflight_passes_a_supplied_config_password_without_echoing_it(self):
        result, _, _, _ = self.run_tool(
            "--check",
            rclone_config_password="test-rclone-config-password",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("portal_pvc_backup=PASS", result.stdout)
        self.assertNotIn("test-rclone-config-password", result.stdout + result.stderr)

    def test_go_keeps_a_supplied_config_password_for_upload_and_remote_restore(self):
        result, calls, _, _ = self.run_tool(
            "--go",
            rclone_config_password="test-rclone-config-password",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.count("rclone copyto"), 2)
        self.assertNotIn("test-rclone-config-password", result.stdout + result.stderr)

    def test_remote_preflight_explains_hidden_password_input_and_has_deadline(self):
        """Interactive operators must not mistake encrypted-rclone input for a hung check."""
        text = SCRIPT.read_text(encoding="utf-8")
        remote_preflight = text[text.index("assert_remote_access() {") : text.index("acquire_lock() {")]

        self.assertIn("portal_pvc_backup_stage=remote_authentication", remote_preflight)
        self.assertIn("[ -t 0 ]", remote_preflight)
        self.assertIn('PORTAL_RCLONE_TIMEOUT_SECONDS:-30', remote_preflight)
        self.assertLess(
            remote_preflight.index("portal_pvc_backup_stage=remote_authentication"),
            remote_preflight.index("rclone lsd"),
        )

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
        result, calls, _, evidence = self.run_tool("--go", require_urllib=True)
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

    def test_restored_portal_health_uses_urllib_and_requires_http_200(self):
        result, calls, _, evidence = self.run_tool("--go")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("portal_pvc_backup=PASS", result.stdout)
        self.assertIn("exec portal-web-1", calls)
        self.assertNotIn("wget", calls)

    def test_lock_contention_uses_local_flock_without_kubernetes_lease_commands(self):
        result, calls, _, _ = self.run_tool("--go", lock_busy=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("portal_pvc_backup_stage=lock", result.stdout)
        self.assertIn("portal_pvc_backup=FAIL", result.stdout)
        self.assertNotIn("scale deployment/portal-web", calls)
        self.assertNotIn("create -f", calls)
        self.assertNotIn("lease", calls)

    def test_non_200_health_blocks_evidence_and_reports_health_failure(self):
        result, calls, _, evidence = self.run_tool("--go", health_status=503)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "portal_pvc_backup_stage=portal_health\nportal_pvc_backup=FAIL")
        self.assertEqual(evidence, "")
        self.assertIn("exec portal-web-1", calls)

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
