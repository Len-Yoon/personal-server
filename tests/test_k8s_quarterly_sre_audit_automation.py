import os
import subprocess
import tempfile
import unittest
import fcntl
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "infra/k8s/tools/quarterly-sre-audit-automation.sh"
SERVICE = ROOT / "infra/k8s/sre-audit-automation/personal-server-quarterly-sre-audit.service.tmpl"
TIMER = ROOT / "infra/k8s/sre-audit-automation/personal-server-quarterly-sre-audit.timer.tmpl"


class QuarterlySreAuditAutomationContractTests(unittest.TestCase):
    def run_controller(
        self,
        action,
        *,
        filesystem="ext4",
        check_results=None,
        hold_run_lock=False,
        backup_evidence=None,
        backup_cronjob_suspended="false",
    ):
        """Run the host controller against only its external command boundary."""
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            state_dir = root / "state"
            unit_dir = root / "units"
            fixture_repo = root / "fixture-repo"
            fixture_tool_dir = fixture_repo / "infra/k8s/tools"
            fixture_template_dir = fixture_repo / "infra/k8s/sre-audit-automation"
            calls = root / "calls.log"
            check_calls = root / "check-calls.log"
            manifest = root / "applied-configmap.yaml"
            for path in (bin_dir, state_dir, unit_dir, fixture_tool_dir, fixture_template_dir):
                path.mkdir(parents=True)
            controller = fixture_tool_dir / CONTROLLER.name
            controller.write_text(CONTROLLER.read_text(encoding="utf-8"), encoding="utf-8")
            controller.chmod(0o755)
            for template in (SERVICE, TIMER):
                target = fixture_template_dir / template.name
                target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
            validator = fixture_tool_dir / "validate-backup-evidence.py"
            validator.write_text(
                (ROOT / "infra/k8s/tools/validate-backup-evidence.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            validator.chmod(0o755)
            def write(name, body):
                path = bin_dir / name
                path.write_text(body, encoding="utf-8")
                path.chmod(0o755)

            def write_check(name, exit_code):
                path = fixture_tool_dir / name
                path.write_text(
                    "#!/bin/sh\nprintf '%s %s\\n' \"$0\" \"$*\" >> \"$CHECK_CALLS\"\n"
                    f"exit {exit_code}\n",
                    encoding="utf-8",
                )
                path.chmod(0o755)

            write(
                "systemctl",
                "#!/bin/sh\nprintf 'systemctl %s\\n' \"$*\" >> \"$CALLS\"\n",
            )
            write(
                "systemd-analyze",
                "#!/bin/sh\nprintf 'systemd-analyze %s\\n' \"$*\" >> \"$CALLS\"\n",
            )
            write(
                "sudo",
                "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "[ \"$1\" = -n ] && shift\nexec \"$@\"\n",
            )
            write(
                "k3s",
                "#!/bin/sh\nprintf 'k3s %s\\n' \"$*\" >> \"$CALLS\"\n"
                "if [ \"$4\" = apply ]; then cat > \"$K3S_MANIFEST\"; fi\n"
                "case \"$*\" in *\"get configmap portal-pvc-backup-evidence\"*) printf '%s' \"$BACKUP_EVIDENCE\" ;; *\"get cronjob portal-pvc-backup\"*) printf '%s' \"$BACKUP_CRONJOB_SUSPENDED\" ;; esac\n",
            )
            write("findmnt", f"#!/bin/sh\nprintf '%s\\n' '{filesystem}'\n")
            write(
                "flock",
                "#!/usr/bin/env python3\n"
                "import fcntl\n"
                "import sys\n"
                "try:\n"
                "    fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "except BlockingIOError:\n"
                "    raise SystemExit(1)\n",
            )
            if check_results is not None:
                for name in (
                    "sre-health-audit.sh",
                    "sre-pod-recovery-lab.sh",
                ):
                    write_check(name, check_results[name])
            now = datetime.now(timezone.utc).replace(microsecond=0)
            valid_evidence = (
                "schema_version=1\nscope=portal\nbackup_status=success\nencrypted=true\n"
                f"backup_completed_at={(now - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
                "restore_status=success\n"
                f"restore_verified_at={now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
                f"evidence_expires_at={(now + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
                "backup_id=portal-test\nsource_runtime=k3s-pvc\n"
            )
            env = {
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "CALLS": str(calls),
                "CHECK_CALLS": str(check_calls),
                "K3S_MANIFEST": str(manifest),
                "QUARTERLY_SRE_AUDIT_STATE_DIR": str(state_dir),
                "QUARTERLY_SRE_AUDIT_UNIT_DIR": str(unit_dir),
                "BACKUP_EVIDENCE": backup_evidence
                if backup_evidence is not None
                else valid_evidence,
                "BACKUP_CRONJOB_SUSPENDED": backup_cronjob_suspended,
            }
            lock_handle = None
            if hold_run_lock:
                lock_file = state_dir / "quarterly-sre-audit.lock"
                lock_handle = lock_file.open("w", encoding="utf-8")
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                result = subprocess.run(
                    ["bash", str(controller), action], env=env, capture_output=True, text=True, check=False
                )
            finally:
                if lock_handle is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                    lock_handle.close()
            rendered_units = {
                path.name: path.read_text(encoding="utf-8") for path in unit_dir.iterdir()
            }
            return (
                result,
                calls.read_text(encoding="utf-8") if calls.exists() else "",
                rendered_units,
                check_calls.read_text(encoding="utf-8") if check_calls.exists() else "",
                manifest.read_text(encoding="utf-8") if manifest.exists() else "",
            )

    def test_controller_contract_is_strict_and_runs_all_checks(self):
        text = CONTROLLER.read_text(encoding="utf-8")
        self.assertIn("set -Eeuo pipefail", text)
        self.assertIn('case "$1" in', text)
        for option in ("--preflight", "--install", "--run", "--status"):
            self.assertIn(option, text)
        self.assertIn("sre-health-audit.sh", text)
        self.assertIn("portal-pvc-backup-evidence", text)
        self.assertIn("validate-backup-evidence.py", text)
        self.assertIn("sre-pod-recovery-lab.sh", text)
        self.assertIn("--run", text)
        self.assertIn("sre-telegram-quarterly-audit-status", text)
        for key in ("run_id", "status", "completed_at", "health_audit", "backup_check", "recovery_lab"):
            self.assertIn(key, text)
        self.assertNotRegex(text, r"cat .*output|command_output|stdout=.*ConfigMap")

    def test_run_executes_every_check_and_applies_failure_status(self):
        result, calls, _, check_calls, manifest = self.run_controller(
            "--run",
            check_results={
                "sre-health-audit.sh": 1,
                "sre-pod-recovery-lab.sh": 1,
            },
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "quarterly_sre_audit=failed\n")
        check_invocations = [line.split(maxsplit=1) for line in check_calls.splitlines()]
        self.assertEqual(
            [(Path(parts[0]).name, parts[1] if len(parts) == 2 else "") for parts in check_invocations],
            [
                ("sre-health-audit.sh", ""),
                ("sre-pod-recovery-lab.sh", "--run"),
            ],
        )
        self.assertIn("sudo -n k3s kubectl -n monitoring apply -f -", calls)
        self.assertIn("status: \"failed\"", manifest)
        self.assertIn("health_audit: \"failed\"", manifest)
        self.assertIn("backup_check: \"passed\"", manifest)
        self.assertIn("recovery_lab: \"failed\"", manifest)
        self.assertNotIn("$(<", manifest)

    def test_run_rejects_lock_contention_before_launching_checks(self):
        result, calls, _, check_calls, manifest = self.run_controller(
            "--run",
            check_results={
                "sre-health-audit.sh": 0,
                "sre-pod-recovery-lab.sh": 0,
            },
            hold_run_lock=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(check_calls, "")
        self.assertNotIn(" apply -f -", calls)
        self.assertEqual(manifest, "")

    def test_run_fails_closed_when_cronjob_backup_evidence_is_invalid(self):
        result, _, _, _, manifest = self.run_controller(
            "--run",
            check_results={
                "sre-health-audit.sh": 0,
                "sre-pod-recovery-lab.sh": 0,
            },
            backup_evidence="schema_version=1\n",
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "quarterly_sre_audit=failed\n")
        self.assertIn('backup_check: "failed"', manifest)

    def test_run_rejects_backup_evidence_from_a_non_k3s_runtime(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        evidence = (
            "schema_version=1\nscope=portal\nbackup_status=success\nencrypted=true\n"
            f"backup_completed_at={(now - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            "restore_status=success\n"
            f"restore_verified_at={now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            f"evidence_expires_at={(now + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            "backup_id=portal-test\nsource_runtime=compose-local\n"
        )
        result, _, _, _, manifest = self.run_controller(
            "--run",
            check_results={"sre-health-audit.sh": 0, "sre-pod-recovery-lab.sh": 0},
            backup_evidence=evidence,
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('backup_check: "failed"', manifest)

    def test_service_is_controller_only_and_hardened(self):
        text = SERVICE.read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", text)
        self.assertIn("ExecStart=@REPO_ROOT@/infra/k8s/tools/quarterly-sre-audit-automation.sh --run", text)
        self.assertNotIn("flock", text)
        self.assertIn("TimeoutStartSec=20min", text)
        self.assertIn("TimeoutStopSec=20s", text)
        for setting in ("PrivateTmp=yes", "ProtectSystem=strict", "ProtectHome=read-only"):
            self.assertIn(setting, text)
        self.assertNotIn("sre-health-audit.sh", text)
        self.assertNotIn("portal-pvc-backup-verify.sh", text)
        self.assertNotIn("sre-pod-recovery-lab.sh", text)

    def test_service_allows_controller_and_state_without_backup_credentials(self):
        text = SERVICE.read_text(encoding="utf-8")
        self.assertIn("ReadOnlyPaths=@REPO_ROOT@", text)
        self.assertIn("ReadWritePaths=@STATE_DIR@", text)
        self.assertNotIn("LoadCredentialEncrypted", text)
        self.assertNotIn("PORTAL_RCLONE_", text)

    def test_service_keeps_restricted_sudo_contract_without_no_new_privileges(self):
        text = SERVICE.read_text(encoding="utf-8")
        self.assertNotIn("NoNewPrivileges=", text)
        self.assertIn("Existing restricted sudo -n k3s permission is required", text)

    def test_preflight_rejects_non_ext4_state_directory(self):
        result, calls, *_ = self.run_controller("--preflight", filesystem="tmpfs")

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("sudo -n k3s", calls)

    def test_preflight_fails_closed_for_missing_malformed_or_stale_backup_evidence(self):
        invalid_records = (
            "",
            "schema_version=1\n",
            "schema_version=1\nscope=portal\nbackup_status=success\nencrypted=true\nbackup_completed_at=2020-01-01T00:00:00Z\nrestore_status=success\nrestore_verified_at=2020-01-01T00:01:00Z\nevidence_expires_at=2099-01-01T00:00:00Z\nbackup_id=portal-test\nsource_runtime=k3s-pvc\n",
        )
        for evidence in invalid_records:
            with self.subTest(evidence=evidence):
                result, calls, *_ = self.run_controller("--preflight", backup_evidence=evidence)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("get cronjob portal-pvc-backup", calls)

    def test_preflight_rejects_suspended_backup_cronjob(self):
        result, calls, *_ = self.run_controller("--preflight", backup_cronjob_suspended="true")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("get cronjob portal-pvc-backup", calls)
        self.assertNotIn("get configmap portal-pvc-backup-evidence", calls)

    def test_install_uses_configmap_evidence_without_backup_credentials(self):
        result, calls, rendered_units, *_ = self.run_controller("--install")

        self.assertEqual(result.returncode, 0, result.stderr)
        service = rendered_units["personal-server-quarterly-sre-audit.service"]
        self.assertNotIn("LoadCredentialEncrypted", service)
        self.assertNotIn("PORTAL_RCLONE_", service)
        self.assertIn("get configmap portal-pvc-backup-evidence", calls)

    def test_installer_verifies_before_reload_and_enables_only_one_timer(self):
        text = CONTROLLER.read_text(encoding="utf-8")
        verify = text.index("systemd-analyze --user verify")
        reload_ = text.index("systemctl --user daemon-reload")
        enable = text.index("systemctl --user enable --now")
        self.assertLess(verify, reload_)
        self.assertLess(reload_, enable)
        self.assertEqual(text.count("systemctl --user enable --now"), 1)
        self.assertNotIn("systemd-creds", text)
        self.assertNotIn("rclone ", text)

    def test_timer_has_exact_quarterly_calendar_and_persistence(self):
        text = TIMER.read_text(encoding="utf-8")
        self.assertIn("Persistent=true", text)
        self.assertIn("OnCalendar=*-01,04,07,10-01 03:30:00 Asia/Seoul", text)
        self.assertIn("Unit=personal-server-quarterly-sre-audit.service", text)


if __name__ == "__main__":
    unittest.main()
