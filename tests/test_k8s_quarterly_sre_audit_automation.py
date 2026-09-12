import os
import subprocess
import tempfile
import unittest
import fcntl
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
        seed_credentials=False,
        check_results=None,
        hold_run_lock=False,
    ):
        """Run the host controller against only its external command boundary."""
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            state_dir = root / "state"
            unit_dir = root / "units"
            credential_dir = root / "backup-credentials"
            fixture_repo = root / "fixture-repo"
            fixture_tool_dir = fixture_repo / "infra/k8s/tools"
            fixture_template_dir = fixture_repo / "infra/k8s/sre-audit-automation"
            calls = root / "calls.log"
            check_calls = root / "check-calls.log"
            manifest = root / "applied-configmap.yaml"
            for path in (bin_dir, state_dir, unit_dir, credential_dir, fixture_tool_dir, fixture_template_dir):
                path.mkdir(parents=True)
            controller = fixture_tool_dir / CONTROLLER.name
            controller.write_text(CONTROLLER.read_text(encoding="utf-8"), encoding="utf-8")
            controller.chmod(0o755)
            for template in (SERVICE, TIMER):
                target = fixture_template_dir / template.name
                target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
            if seed_credentials:
                (credential_dir / "rclone-config.cred").write_text("host-encrypted-config", encoding="utf-8")
                (credential_dir / "rclone-config-passphrase.cred").write_text(
                    "host-encrypted-passphrase", encoding="utf-8"
                )

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
                "if [ \"$4\" = apply ]; then cat > \"$K3S_MANIFEST\"; fi\n",
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
                    "portal-pvc-backup-verify.sh",
                    "sre-pod-recovery-lab.sh",
                ):
                    write_check(name, check_results[name])
            env = {
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "CALLS": str(calls),
                "CHECK_CALLS": str(check_calls),
                "K3S_MANIFEST": str(manifest),
                "QUARTERLY_SRE_AUDIT_STATE_DIR": str(state_dir),
                "QUARTERLY_SRE_AUDIT_UNIT_DIR": str(unit_dir),
                "QUARTERLY_SRE_AUDIT_BACKUP_CREDENTIAL_DIR": str(credential_dir),
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
            credential_contents = {
                path.name: path.read_text(encoding="utf-8") for path in credential_dir.iterdir()
            }
            return (
                result,
                calls.read_text(encoding="utf-8") if calls.exists() else "",
                rendered_units,
                credential_contents,
                str(credential_dir),
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
        self.assertIn("portal-pvc-backup-verify.sh", text)
        self.assertIn("--check", text)
        self.assertIn("sre-pod-recovery-lab.sh", text)
        self.assertIn("--run", text)
        self.assertIn("sre-telegram-quarterly-audit-status", text)
        for key in ("run_id", "status", "completed_at", "health_audit", "backup_check", "recovery_lab"):
            self.assertIn(key, text)
        self.assertNotRegex(text, r"cat .*output|command_output|stdout=.*ConfigMap")

    def test_run_executes_every_check_and_applies_failure_status(self):
        result, calls, _, _, _, check_calls, manifest = self.run_controller(
            "--run",
            check_results={
                "sre-health-audit.sh": 1,
                "portal-pvc-backup-verify.sh": 0,
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
                ("portal-pvc-backup-verify.sh", "--check"),
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
        result, calls, _, _, _, check_calls, manifest = self.run_controller(
            "--run",
            check_results={
                "sre-health-audit.sh": 0,
                "portal-pvc-backup-verify.sh": 0,
                "sre-pod-recovery-lab.sh": 0,
            },
            hold_run_lock=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(check_calls, "")
        self.assertNotIn(" apply -f -", calls)
        self.assertEqual(manifest, "")

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

    def test_service_allows_backup_check_read_only_inputs_and_state(self):
        text = SERVICE.read_text(encoding="utf-8")
        self.assertIn("ReadOnlyPaths=@REPO_ROOT@ %h/.local/share/personal-server/age", text)
        self.assertIn("ReadWritePaths=@STATE_DIR@", text)

    def test_service_keeps_restricted_sudo_contract_without_no_new_privileges(self):
        text = SERVICE.read_text(encoding="utf-8")
        self.assertNotIn("NoNewPrivileges=", text)
        self.assertIn("Existing restricted sudo -n k3s permission is required", text)

    def test_preflight_rejects_non_ext4_state_directory(self):
        result, calls, *_ = self.run_controller("--preflight", filesystem="tmpfs")

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("sudo -n k3s", calls)

    def test_install_reuses_existing_encrypted_backup_credentials_without_reading_values(self):
        result, calls, rendered_units, credential_contents, credential_prefix, *_ = self.run_controller(
            "--install", seed_credentials=True
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        service = rendered_units["personal-server-quarterly-sre-audit.service"]
        self.assertIn(f"LoadCredentialEncrypted=rclone-config:{credential_prefix}/rclone-config.cred", service)
        self.assertIn(
            f"LoadCredentialEncrypted=rclone-config-passphrase:{credential_prefix}/rclone-config-passphrase.cred",
            service,
        )
        self.assertIn("Environment=\"PORTAL_RCLONE_CONFIG_FILE=%d/rclone-config\"", service)
        self.assertIn(
            "Environment=\"PORTAL_RCLONE_PASSWORD_COMMAND=/usr/bin/cat %d/rclone-config-passphrase\"",
            service,
        )
        self.assertEqual(credential_contents["rclone-config.cred"], "host-encrypted-config")
        self.assertEqual(credential_contents["rclone-config-passphrase.cred"], "host-encrypted-passphrase")
        self.assertNotIn("host-encrypted-config", service + calls)
        self.assertNotIn("host-encrypted-passphrase", service + calls)

    def test_install_fails_closed_when_reused_backup_credentials_are_missing(self):
        result, calls, *_ = self.run_controller("--install")

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("enable --now", calls)

    def test_preflight_requires_reused_backup_credentials_before_installation_checks(self):
        result, calls, *_ = self.run_controller("--preflight")

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("sudo -n k3s", calls)

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
