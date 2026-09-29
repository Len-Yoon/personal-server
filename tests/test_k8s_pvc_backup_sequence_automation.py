import os
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/k8s/tools/pvc-backup-sequence-automation.sh"
TEMPLATES = ROOT / "infra/k8s/backup-automation"


class PvcBackupSequenceAutomationTests(unittest.TestCase):
    def test_timer_has_no_missed_run_catchup(self):
        timer = (TEMPLATES / "pvc-backup-sequence.timer.tmpl").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 00:30:00 Asia/Seoul", timer)
        self.assertIn("Persistent=false", timer)

    def test_service_runs_one_coordinator_with_bounded_retries(self):
        service = (TEMPLATES / "pvc-backup-sequence.service.tmpl").read_text(encoding="utf-8")
        self.assertIn("--go", service)
        self.assertIn("Restart=on-failure", service)
        self.assertIn("StartLimitBurst=3", service)

    def run_action(self, action, *, suspended="true", deadline="300", legacy="inactive", legacy_service="inactive", legacy_pid="0", sequence_service="inactive", sequence_pid="0", sequence_status="", active_from="", patch_exit="0", timer_active="0", jobs_json='{"items":[]}', runner_check_exit="0", lock_check_exit="0"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            unit_dir = root / "units"
            bin_dir.mkdir()
            unit_dir.mkdir()
            for name in ("personal-server-pvc-backup-sequence.service", "personal-server-pvc-backup-sequence.timer"):
                (unit_dir / name).write_text("fixture", encoding="utf-8")
            calls = root / "calls"

            def fake(name, body):
                path = bin_dir / name
                path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
                path.chmod(0o755)

            fake("sudo", '[ "$1" = -n ] && shift\nexec "$@"\n')
            fake("python3", '''case "$*" in
  *'pvc-backup-sequence.py --check-lock'*) printf 'runner --check-lock\\n' >> "$CALLS"; exit "$LOCK_CHECK_EXIT" ;;
  *'pvc-backup-sequence.py --check'*) printf 'runner --check\\n' >> "$CALLS"; exit "$RUNNER_CHECK_EXIT" ;;
esac
exec "$REAL_PYTHON" "$@"
''')
            fake("k3s", '''printf 'k3s %s\\n' "$*" >> "$CALLS"
case "$*" in
  *'get nodes'*) exit 0 ;;
  *'get configmap pvc-backup-sequence-state -o name'*) printf 'configmap/pvc-backup-sequence-state\\n' ;;
  *'get configmap pvc-backup-sequence-state -o jsonpath={.data.status}'*) printf '%s' "$SEQUENCE_STATUS" ;;
  *'get configmap pvc-backup-sequence-state -o jsonpath='*) printf '%s' "$ACTIVE_FROM" ;;
  *'get cronjob '*' -o jsonpath={.spec.startingDeadlineSeconds}'*) printf '%s' "$DEADLINE" ;;
  *'get cronjob '*' -o jsonpath='*) printf '%s' "$SUSPENDED" ;;
  *'get jobs -o json'*) printf '%s' "$JOBS_JSON" ;;
  *'patch configmap pvc-backup-sequence-state'*) exit "$PATCH_EXIT" ;;
esac
''')
            fake("loginctl", 'printf "yes\\n"\n')
            fake("systemctl", '''printf 'systemctl %s\\n' "$*" >> "$CALLS"
case "$*" in
  *'show-environment'*) exit 0 ;;
  *'is-enabled personal-server-portal-pvc-backup.timer'*) printf 'disabled\\n' ;;
  *'is-active personal-server-portal-pvc-backup.timer'*) printf '%s\\n' "$LEGACY" ;;
  *'show personal-server-portal-pvc-backup.service --property=ActiveState --value'*) printf '%s\\n' "$LEGACY_SERVICE" ;;
  *'show personal-server-portal-pvc-backup.service --property=MainPID --value'*) printf '%s\\n' "$LEGACY_PID" ;;
  *'show personal-server-pvc-backup-sequence.service --property=ActiveState --value'*) printf '%s\\n' "$SEQUENCE_SERVICE" ;;
  *'show personal-server-pvc-backup-sequence.service --property=MainPID --value'*) printf '%s\\n' "$SEQUENCE_PID" ;;
  *'is-active --quiet personal-server-pvc-backup-sequence.service'*) [ "$SEQUENCE_SERVICE" = active ] ;;
  *'is-active --quiet personal-server-pvc-backup-sequence.timer'*) exit "$TIMER_ACTIVE" ;;
esac
''')
            fake("systemd-analyze", 'exit 0\n')
            env = {
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "PVC_BACKUP_SEQUENCE_UNIT_DIR": str(unit_dir),
                "CALLS": str(calls),
                "SUSPENDED": suspended,
                "DEADLINE": deadline,
                "LEGACY": legacy,
                "LEGACY_SERVICE": legacy_service,
                "LEGACY_PID": legacy_pid,
                "SEQUENCE_SERVICE": sequence_service,
                "SEQUENCE_PID": sequence_pid,
                "SEQUENCE_STATUS": sequence_status,
                "ACTIVE_FROM": active_from,
                "PATCH_EXIT": patch_exit,
                "TIMER_ACTIVE": timer_active,
                "JOBS_JSON": jobs_json,
                "RUNNER_CHECK_EXIT": runner_check_exit,
                "LOCK_CHECK_EXIT": lock_check_exit,
                "REAL_PYTHON": sys.executable,
            }
            result = subprocess.run(["bash", str(SCRIPT), action], env=env, capture_output=True, text=True, check=False)
            return result, calls.read_text(encoding="utf-8") if calls.exists() else ""

    def test_activation_refuses_any_unsuspended_cronjob(self):
        result, calls = self.run_action("--activate", suspended="false")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("enable --now", calls)

    def test_activation_refuses_missing_start_deadline(self):
        result, calls = self.run_action("--activate", deadline="")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("enable --now", calls)

    def test_activation_refuses_pending_manual_backup_with_unrelated_name(self):
        jobs = '{"items":[{"metadata":{"name":"manual-verify-1"},"spec":{"template":{"spec":{"serviceAccountName":"book-pvc-backup"}}},"status":{}}]}'
        result, calls = self.run_action("--activate", jobs_json=jobs)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("enable --now", calls)

    def test_activation_refuses_live_template_drift(self):
        result, calls = self.run_action("--activate", runner_check_exit="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runner --check", calls)
        self.assertNotIn("enable --now", calls)

    def test_activation_refuses_unavailable_user_lock(self):
        result, calls = self.run_action("--activate", lock_check_exit="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runner --check-lock", calls)
        self.assertNotIn("enable --now", calls)

    def test_activation_refuses_active_legacy_timer(self):
        result, calls = self.run_action("--activate", legacy="active")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("enable --now", calls)

    def test_activation_refuses_running_legacy_service(self):
        result, calls = self.run_action("--activate", legacy_service="active", legacy_pid="1234")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("enable --now", calls)

    def test_activation_refuses_existing_active_from(self):
        result, calls = self.run_action("--activate", active_from="2026-09-29")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("enable --now", calls)

    def test_activation_rolls_back_timer_if_state_patch_fails(self):
        result, calls = self.run_action("--activate", patch_exit="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("enable --now", calls)
        self.assertIn("disable --now", calls)

    def test_activation_succeeds_only_when_timer_and_state_ready(self):
        result, calls = self.run_action("--activate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("patch configmap pvc-backup-sequence-state", calls)
        self.assertIn('deactivated_at', calls)
        self.assertIn("enable --now", calls)

    def test_deactivation_records_explicit_stop_for_relay(self):
        result, calls = self.run_action("--deactivate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("disable --now", calls)
        self.assertIn('deactivated_at', calls)
        self.assertIn('active_from', calls)

    def test_deactivation_keeps_monitoring_when_job_is_running(self):
        result, calls = self.run_action("--deactivate", sequence_service="active", sequence_pid="1234")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("disable --now", calls)
        self.assertNotIn("patch configmap pvc-backup-sequence-state", calls)

    def test_deactivation_keeps_monitoring_during_restart_delay(self):
        result, calls = self.run_action("--deactivate", sequence_service="activating", sequence_pid="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("patch configmap pvc-backup-sequence-state", calls)

    def test_deactivation_keeps_monitoring_when_result_is_running(self):
        result, calls = self.run_action("--deactivate", sequence_status="running")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("patch configmap pvc-backup-sequence-state", calls)


if __name__ == "__main__":
    unittest.main()
