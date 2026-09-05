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
    def run_tool(self, mode="--go", *, runtime="k3s", fail_at="", missing_pvc=False, repeat=False, second_runtime=None, namespace=None, existing_evidence="", special_entry=False, send_signal=False, followup_signal=False, lock_busy=False, require_urllib=False, health_status=200, rclone_config_file="", rclone_password_command="", assert_lock_fd_closed=False, hang_stream=False, signal_when="reader", assert_stream_child_stopped=False):
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
                "PORTAL_BACKUP_STATE_DIR": str(root / "backup-state"),
                "PORTAL_AGE_RECIPIENT": str(root / "recipient.txt"),
                "PORTAL_AGE_IDENTITY": str(root / "identity.txt"),
                "PORTAL_BACKUP_REMOTE": f"fake:{remote}",
                "PORTAL_FAKE_FAIL_AT": fail_at,
                "PORTAL_FAKE_MISSING_PVC": "1" if missing_pvc else "",
                "PORTAL_FAKE_SPECIAL_ENTRY": "1" if special_entry else "",
                "PORTAL_FAKE_HOLD": "1" if send_signal and signal_when == "reader" else "",
                "PORTAL_FAKE_READER_WAIT": str(root / "reader-wait"),
                "PORTAL_FAKE_HANG_STREAM": "1" if hang_stream else "",
                "PORTAL_FAKE_STREAM_WAIT": str(root / "stream-wait"),
                "PORTAL_FAKE_STREAM_CHILD_PID": str(root / "stream-child.pid"),
                "PORTAL_FAKE_CLEANUP_DELETE_WAIT": str(root / "cleanup-delete-wait"),
                "PORTAL_FAKE_HOLD_CLEANUP_DELETE": "1" if followup_signal else "",
                "PORTAL_FAKE_NOISY": "1",
                "PORTAL_FAKE_LOCK_BUSY": "1" if lock_busy else "",
                "PORTAL_FAKE_REQUIRE_URLLIB": "1" if require_urllib else "",
                "PORTAL_FAKE_HEALTH_STATUS": str(health_status),
                "PORTAL_RCLONE_CONFIG_FILE": rclone_config_file,
                "PORTAL_RCLONE_PASSWORD_COMMAND": rclone_password_command,
                "PORTAL_FAKE_ASSERT_LOCK_FD_CLOSED": "1" if assert_lock_fd_closed else "",
            }
            if namespace is not None:
                env["PORTAL_NAMESPACE"] = namespace
            if send_signal:
                process = subprocess.Popen(["bash", str(SCRIPT), mode], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                reader_wait = root / ("stream-wait" if signal_when == "stream" else "reader-wait")
                deadline = time.time() + 5
                while not reader_wait.exists() and time.time() < deadline:
                    time.sleep(0.01)
                self.assertTrue(reader_wait.exists(), "signal boundary was not reached")
                process.send_signal(signal.SIGTERM)
                if followup_signal:
                    cleanup_delete_wait = root / "cleanup-delete-wait"
                    deadline = time.time() + 8
                    while not cleanup_delete_wait.exists() and time.time() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(cleanup_delete_wait.exists(), "cleanup did not begin reader deletion")
                    process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=10)
                result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
                if assert_stream_child_stopped:
                    child_pid_file = root / "stream-child.pid"
                    self.assertTrue(child_pid_file.exists(), "fake stream child did not start")
                    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
                    deadline = time.time() + 2
                    while self.process_is_live(child_pid) and time.time() < deadline:
                        time.sleep(0.01)
                    self.assertFalse(self.process_is_live(child_pid), "interrupted stream child survived")
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
    def process_is_live(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def write_fakes(bin_dir, root, calls, manifest, files, state, remote):
        def write(name, text):
            path = bin_dir / name
            path.write_text(text, encoding="utf-8")
            path.chmod(0o755)

        write(
            "sudo",
            "#!/bin/sh\nif [ \"${1:-}\" = -n ]; then shift; fi\nif [ \"${1:-}\" = -v ]; then exit 0; fi\nexec \"$@\"\n",
        )
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
if [ "${{PORTAL_FAKE_ASSERT_LOCK_FD_CLOSED:-}}" = 1 ]; then
  case "$*" in
    *'exec -i'*)
      if (: >&9) 2>/dev/null; then exit 42; fi
      ;;
  esac
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
  *'delete pod'*)
    if [ "${{PORTAL_FAKE_HOLD_CLEANUP_DELETE:-}}" = 1 ]; then
      touch "${{PORTAL_FAKE_CLEANUP_DELETE_WAIT}}"
      sleep 1
    fi
    exit 0 ;;
  *'exec -i'*'/data/files'*)
    if [ "${{PORTAL_FAKE_HANG_STREAM:-}}" = 1 ]; then
      touch "${{PORTAL_FAKE_STREAM_WAIT}}"
      sleep 60 &
      echo "$!" > "${{PORTAL_FAKE_STREAM_CHILD_PID}}"
      wait "$!"
      exit 0
    fi
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
while [ "$#" -gt 0 ]; do
  case "$1" in
    --config|--password-command|--log-level) shift 2 ;;
    --immutable) shift ;;
    *) break ;;
  esac
done
operation=$1
if [ "${{PORTAL_FAKE_FAIL_AT:-}}" = remote ] && [ "$operation" = lsd ]; then
  printf '%s\\n' 'fake-remote-secret /private/noisy/path' >&2
  exit 42
fi
if [ "${{PORTAL_FAKE_ASSERT_LOCK_FD_CLOSED:-}}" = 1 ] && [ "$operation" = copyto ]; then
  fd_mode=$(python3 -c 'import fcntl, os; print(fcntl.fcntl(9, fcntl.F_GETFL) & os.O_ACCMODE)' 2>/dev/null) || exit 42
  [ "$fd_mode" = 0 ] || exit 42
fi
if [ "$operation" = lsd ]; then exit 0; fi
if [ "${{PORTAL_FAKE_FAIL_AT:-}}" = upload ] && [ "$operation" = copyto ]; then
  printf '%s\\n' 'fake-upload-secret /private/noisy/path' >&2
  exit 42
fi
shift
while [ "$#" -gt 0 ]; do
  case "$1" in
    --log-level) shift 2 ;;
    --immutable) shift ;;
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

    def test_kubernetes_commands_use_the_limited_noninteractive_k3s_sudo_grant(self):
        """Automatic backups may only use the existing passwordless k3s sudo grant."""
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("sudo -n k3s kubectl", text)
        self.assertNotIn("sudo -n true", text)
        self.assertNotIn("sudo -v", text)
        self.assertLess(text.index("ensure_sudo_access || exit 1"), text.index("assert_preflight || exit 1"))

    def test_credential_paths_are_passed_to_rclone_without_a_password_environment_variable(self):
        """The systemd credential boundary must reach rclone without exposing a secret."""
        text = SCRIPT.read_text(encoding="utf-8")
        remote_preflight = text[text.index("assert_remote_access() {") : text.index("acquire_lock() {")]

        self.assertIn("PORTAL_RCLONE_CONFIG_FILE", text)
        self.assertIn("PORTAL_RCLONE_PASSWORD_COMMAND", text)
        self.assertIn("--password-command", text)
        self.assertNotIn("RCLONE_CONFIG_PASS", text)
        self.assertIn("--config", text)

    def test_remote_preflight_passes_credential_file_paths_without_echoing_a_password(self):
        with tempfile.TemporaryDirectory() as directory:
            credential_dir = Path(directory)
            config_file = credential_dir / "rclone-config"
            password_file = credential_dir / "rclone-config-passphrase"
            config_file.write_text("encrypted-config", encoding="utf-8")
            password_file.write_text("passphrase", encoding="utf-8")
            result, calls, _, _ = self.run_tool_with_rclone_credentials(config_file, password_file, "--check")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("portal_pvc_backup=PASS", result.stdout)
        self.assertIn(f"--config {config_file}", calls)
        self.assertIn(f"--password-command /usr/bin/cat {password_file}", calls)
        self.assertNotIn("passphrase", result.stdout + result.stderr)

    def test_go_passes_credential_file_paths_for_upload_and_remote_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            credential_dir = Path(directory)
            config_file = credential_dir / "rclone-config"
            password_file = credential_dir / "rclone-config-passphrase"
            config_file.write_text("encrypted-config", encoding="utf-8")
            password_file.write_text("passphrase", encoding="utf-8")
            result, calls, _, _ = self.run_tool_with_rclone_credentials(config_file, password_file, "--go")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.count(" copyto"), 2)
        self.assertEqual(calls.count(f"--config {config_file}"), 3)
        self.assertEqual(calls.count(f"--password-command /usr/bin/cat {password_file}"), 3)
        self.assertNotIn("passphrase", result.stdout + result.stderr)

    def run_tool_with_rclone_credentials(self, config_file, password_file, mode):
        return self.run_tool(
            mode,
            rclone_config_file=str(config_file),
            rclone_password_command=f"/usr/bin/cat {password_file}",
        )

    def test_remote_preflight_uses_rclone_password_command_boundary_and_has_deadline(self):
        """The automated path must receive only a credential command and deadline."""
        text = SCRIPT.read_text(encoding="utf-8")
        remote_preflight = text[text.index("assert_remote_access() {") : text.index("acquire_lock() {")]

        self.assertIn('PORTAL_RCLONE_TIMEOUT_SECONDS:-30', remote_preflight)
        self.assertIn("rclone_with_credentials_timeout", remote_preflight)
        self.assertNotIn("RCLONE_CONFIG_PASS", remote_preflight)

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
        self.assertEqual(result.stdout.strip(), "\n".join((
            "portal_pvc_backup_stage=writer_pause",
            "portal_pvc_backup_stage=pvc_snapshot",
            "portal_pvc_backup_stage=remote_upload",
            "portal_pvc_backup_stage=remote_restore",
            "portal_pvc_backup_stage=restore_validation",
            "portal_pvc_backup=FAIL",
        )))
        self.assertIn("kubectl -n personal-server delete pod", calls)

    def test_signal_forces_fail_after_cleanup(self):
        result, calls, _, _ = self.run_tool("--go", send_signal=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("portal_pvc_backup=PASS", result.stdout)
        self.assertIn("scale deployment/portal-web --replicas=1", calls)

    def test_followup_signal_during_cleanup_still_deletes_reader_and_restores_portal(self):
        result, calls, _, _ = self.run_tool("--go", send_signal=True, followup_signal=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("kubectl -n personal-server delete pod", calls)
        self.assertIn("scale deployment/portal-web --replicas=1", calls)

    def test_cleanup_ignores_followup_termination_signals_until_portal_is_restored(self):
        text = SCRIPT.read_text(encoding="utf-8")
        cleanup = text[text.index("cleanup() {") : text.index('case "${1:-}" in')]

        self.assertIn("trap '' INT TERM HUP", cleanup)
        self.assertNotIn("trap - EXIT INT TERM HUP", cleanup)

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

    def test_go_reports_fixed_progress_stages_without_private_diagnostics(self):
        result, _, _, _ = self.run_tool("--go")

        self.assertEqual(result.returncode, 0, result.stderr)
        for stage in (
            "portal_pvc_backup_stage=writer_pause",
            "portal_pvc_backup_stage=pvc_snapshot",
            "portal_pvc_backup_stage=remote_upload",
            "portal_pvc_backup_stage=remote_restore",
            "portal_pvc_backup_stage=restore_validation",
        ):
            self.assertIn(stage, result.stdout)
        self.assertNotIn("/tmp/", result.stdout)

    def test_rollout_failure_removes_evidence_and_reports_fixed_failure(self):
        result, calls, _, evidence = self.run_tool("--go", fail_at="rollout")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "\n".join((
            "portal_pvc_backup_stage=writer_pause",
            "portal_pvc_backup_stage=pvc_snapshot",
            "portal_pvc_backup_stage=remote_upload",
            "portal_pvc_backup_stage=remote_restore",
            "portal_pvc_backup_stage=restore_validation",
            "portal_pvc_backup_stage=portal_readiness",
            "portal_pvc_backup=FAIL",
        )))
        self.assertEqual(evidence, "")
        self.assertIn("scale deployment/portal-web --replicas=1", calls)

    def test_health_failure_removes_evidence_and_reports_fixed_failure(self):
        result, calls, _, evidence = self.run_tool("--go", fail_at="health")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "\n".join((
            "portal_pvc_backup_stage=writer_pause",
            "portal_pvc_backup_stage=pvc_snapshot",
            "portal_pvc_backup_stage=remote_upload",
            "portal_pvc_backup_stage=remote_restore",
            "portal_pvc_backup_stage=restore_validation",
            "portal_pvc_backup_stage=portal_health",
            "portal_pvc_backup=FAIL",
        )))
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
        self.assertEqual(result.stdout.strip(), "\n".join((
            "portal_pvc_backup_stage=writer_pause",
            "portal_pvc_backup_stage=pvc_snapshot",
            "portal_pvc_backup_stage=remote_upload",
            "portal_pvc_backup_stage=remote_restore",
            "portal_pvc_backup_stage=restore_validation",
            "portal_pvc_backup_stage=portal_health",
            "portal_pvc_backup=FAIL",
        )))
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
        self.assertIn("portal_pvc_backup_stage=pvc_snapshot", result.stdout)
        self.assertNotIn("portal_pvc_backup_stage=remote_upload", result.stdout)
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

    def test_go_does_not_pass_the_backup_lock_to_stream_or_remote_transfer_children(self):
        """Interrupted child commands must not retain the parent backup lock."""
        result, _, _, _ = self.run_tool("--go", assert_lock_fd_closed=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("portal_pvc_backup=PASS", result.stdout)

    def test_timeout_supervisor_terminates_the_entire_kubernetes_command_session(self):
        """A timed-out kubectl exec must not leave a reader process behind."""
        text = SCRIPT.read_text(encoding="utf-8")
        timeout_helper = text[text.index("run_timeout() {") : text.index("kctl() {")]

        self.assertIn('python3 -c "$TIMEOUT_SUPERVISOR" "$seconds" "$@" 9>&-', timeout_helper)
        self.assertIn('run_unlocked() { "$@" 9</dev/null; }', text)
        self.assertIn("process_group=0", text)
        self.assertNotIn("start_new_session=True", text)
        self.assertIn("os.killpg(process.pid, signal_to_send)", text)
        self.assertIn("process.wait(timeout=seconds)", text)
        self.assertIn("PR_SET_PDEATHSIG = 1", text)
        self.assertIn("libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)", text)
        self.assertIn("signal.signal(signal.SIGINT, on_signal)", text)

    def test_signal_terminates_an_in_flight_pvc_stream_process_group(self):
        """Interrupting the controller must also stop its live kubectl stream child."""
        result, _, _, _ = self.run_tool(
            "--go",
            send_signal=True,
            hang_stream=True,
            signal_when="stream",
            assert_stream_child_stopped=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("portal_pvc_backup=PASS", result.stdout)

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
