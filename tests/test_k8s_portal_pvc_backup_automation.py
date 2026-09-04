import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "k8s" / "tools" / "portal-pvc-backup-automation.sh"
SERVICE_TEMPLATE = (
    ROOT / "infra" / "k8s" / "backup-automation" / "personal-server-portal-pvc-backup.service.tmpl"
)
TIMER_TEMPLATE = (
    ROOT / "infra" / "k8s" / "backup-automation" / "personal-server-portal-pvc-backup.timer.tmpl"
)


class PortalPvcBackupAutomationTests(unittest.TestCase):
    def run_tool(self, action, *, backup_output="backup_upload=UPLOADED\n", backup_exit=0, seed_credentials=False, systemctl_stop_exit=0, systemctl_disable_exit=0, systemd_analyze_exit=0):
        self.assertTrue(SCRIPT.is_file(), "backup automation controller is required")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            state_dir = root / "state"
            credential_dir = root / "credentials"
            unit_dir = root / "units"
            source_config = root / "rclone.conf"
            configmap = root / "configmap.yaml"
            calls = root / "calls.log"
            for path in (bin_dir, state_dir, credential_dir, unit_dir):
                path.mkdir(parents=True)
            source_config.write_text("[remote]\ntype = drive\n", encoding="utf-8")
            if seed_credentials:
                (credential_dir / "rclone-config.cred").write_text("host-encrypted", encoding="utf-8")
                (credential_dir / "rclone-config-passphrase.cred").write_text("host-encrypted", encoding="utf-8")
            self.write_fakes(bin_dir)
            env = {
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "PORTAL_BACKUP_AUTOMATION_STATE_DIR": str(state_dir),
                "PORTAL_BACKUP_AUTOMATION_CREDENTIAL_DIR": str(credential_dir),
                "PORTAL_BACKUP_AUTOMATION_UNIT_DIR": str(unit_dir),
                "PORTAL_RCLONE_SOURCE_CONFIG": str(source_config),
                "PORTAL_BACKUP_TOOL": str(bin_dir / "portal-pvc-backup-verify.sh"),
                "PORTAL_BACKUP_AUTOMATION_CONFIGMAP_CAPTURE": str(configmap),
                "CALLS": str(calls),
                "BACKUP_OUTPUT": backup_output,
                "BACKUP_EXIT": str(backup_exit),
                "SYSTEMCTL_STOP_EXIT": str(systemctl_stop_exit),
                "SYSTEMCTL_DISABLE_EXIT": str(systemctl_disable_exit),
                "SYSTEMD_ANALYZE_EXIT": str(systemd_analyze_exit),
            }
            result = subprocess.run(
                ["bash", str(SCRIPT), action],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            credential_files = {
                path.name: path.read_text(encoding="utf-8")
                for path in credential_dir.iterdir()
            }
            return result, configmap.read_text(encoding="utf-8") if configmap.exists() else "", calls.read_text(encoding="utf-8") if calls.exists() else "", credential_files

    @staticmethod
    def write_fakes(bin_dir):
        def write(name, body):
            path = bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)

        write(
            "portal-pvc-backup-verify.sh",
            "#!/bin/sh\nprintf '%s' \"${BACKUP_OUTPUT}\"\nexit \"${BACKUP_EXIT}\"\n",
        )
        write(
            "sudo",
            "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
            "[ \"$1\" = -n ] && shift\nexec \"$@\"\n",
        )
        write(
            "k3s",
            "#!/bin/sh\nprintf 'k3s %s\\n' \"$*\" >> \"$CALLS\"\n"
            "case \"$*\" in\n"
            "  *'apply -f -'*) cat > \"$PORTAL_BACKUP_AUTOMATION_CONFIGMAP_CAPTURE\" ;;\n"
            "  *'get configmap'*) exit 0 ;;\n"
            "esac\n",
        )
        write(
            "systemd-ask-password",
            "#!/bin/sh\nprintf 'rclone-passphrase'\n",
        )
        write(
            "systemd-creds",
            "#!/bin/sh\nprintf 'creds %s\\n' \"$*\" >> \"$CALLS\"\n"
            "case \"$*\" in *' - '*) cat >/dev/null ;; esac\n"
            "for last; do :; done\nprintf 'host-encrypted' > \"$last\"\n",
        )
        write(
            "systemctl",
            "#!/bin/sh\nprintf 'systemctl %s\\n' \"$*\" >> \"$CALLS\"\n"
            "case \"$*\" in *' disable --now personal-server-portal-pvc-backup.timer'*) exit \"${SYSTEMCTL_DISABLE_EXIT:-0}\" ;; esac\n"
            "case \"$*\" in *' stop personal-server-portal-pvc-backup.service'*) exit \"${SYSTEMCTL_STOP_EXIT:-0}\" ;; esac\n",
        )
        write("rclone", "#!/bin/sh\nexit 0\n")
        write("findmnt", "#!/bin/sh\nprintf 'ext4\\n'\n")
        write(
            "systemd-analyze",
            "#!/bin/sh\nprintf 'systemd-analyze %s\\n' \"$*\" >> \"$CALLS\"\n"
            "exit \"${SYSTEMD_ANALYZE_EXIT:-0}\"\n",
        )

    def test_run_writes_exact_completed_configmap_schema_from_upload_success(self):
        result, configmap, calls, _ = self.run_tool("--run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("name: sre-telegram-backup-status", configmap)
        self.assertIn("namespace: monitoring", configmap)
        self.assertIn("run_id:", configmap)
        self.assertIn('status: "completed"', configmap)
        self.assertIn("completed_at:", configmap)
        self.assertIn('stage: "completed"', configmap)
        self.assertIn("sudo -n k3s kubectl -n monitoring apply -f -", calls)
        self.assertNotIn("rclone-passphrase", result.stdout + result.stderr + configmap + calls)

    def test_run_classifies_unchanged_backup_without_upload_as_unchanged(self):
        result, configmap, _, _ = self.run_tool("--run", backup_output="backup_upload=SKIPPED_UNCHANGED\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('status: "unchanged"', configmap)
        self.assertIn('stage: "unchanged"', configmap)

    def test_run_classifies_restore_validation_failure_and_returns_the_backup_failure(self):
        result, configmap, _, _ = self.run_tool(
            "--run",
            backup_output="portal_pvc_backup_stage=restore_validation\nportal_pvc_backup=FAIL\n",
            backup_exit=1,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn('status: "restore_failed"', configmap)
        self.assertIn('stage: "restore-validation"', configmap)

    def test_enroll_creates_only_host_encrypted_credentials_from_masked_input(self):
        result, _, calls, credential_files = self.run_tool("--enroll")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(credential_files["rclone-config.cred"], "host-encrypted")
        self.assertEqual(credential_files["rclone-config-passphrase.cred"], "host-encrypted")
        self.assertIn("--with-key=host --name=rclone-config", calls)
        self.assertIn("--with-key=host --name=rclone-config-passphrase", calls)
        self.assertNotIn("rclone-passphrase", calls)
        self.assertEqual(set(credential_files), {"rclone-config.cred", "rclone-config-passphrase.cred"})

    def test_uninstall_disables_the_only_timer_before_removing_credentials_and_status(self):
        result, _, calls, credential_files = self.run_tool("--uninstall", seed_credentials=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(credential_files, {})
        self.assertIn("systemctl --user disable --now personal-server-portal-pvc-backup.timer", calls)
        self.assertIn("systemctl --user stop personal-server-portal-pvc-backup.service", calls)
        self.assertIn("sudo -n k3s kubectl -n monitoring delete configmap sre-telegram-backup-status --ignore-not-found", calls)
        self.assertLess(calls.index("disable --now"), calls.index("stop personal-server-portal-pvc-backup.service"))
        self.assertLess(calls.index("stop personal-server-portal-pvc-backup.service"), calls.index("delete configmap"))

    def test_uninstall_retains_credentials_and_status_when_active_service_cannot_stop(self):
        result, _, calls, credential_files = self.run_tool(
            "--uninstall", seed_credentials=True, systemctl_stop_exit=1
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(set(credential_files), {"rclone-config.cred", "rclone-config-passphrase.cred"})
        self.assertNotIn("delete configmap", calls)

    def test_uninstall_retains_credentials_and_status_when_timer_cannot_stop(self):
        result, _, calls, credential_files = self.run_tool(
            "--uninstall", seed_credentials=True, systemctl_disable_exit=1
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(set(credential_files), {"rclone-config.cred", "rclone-config-passphrase.cred"})
        self.assertNotIn("stop personal-server-portal-pvc-backup.service", calls)
        self.assertNotIn("delete configmap", calls)

    def test_install_verifies_rendered_user_units_before_enabling_timer(self):
        result, _, calls, _ = self.run_tool("--install", seed_credentials=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("systemd-analyze --user verify", calls)
        self.assertIn("personal-server-portal-pvc-backup.service", calls)
        self.assertIn("personal-server-portal-pvc-backup.timer", calls)
        self.assertIn("systemctl --user enable --now personal-server-portal-pvc-backup.timer", calls)
        self.assertLess(calls.index("systemd-analyze --user verify"), calls.index("enable --now"))

    def test_install_fails_closed_when_rendered_unit_verification_fails(self):
        result, _, calls, _ = self.run_tool(
            "--install", seed_credentials=True, systemd_analyze_exit=1
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("systemd-analyze --user verify", calls)
        self.assertNotIn("enable --now", calls)

    def test_service_template_keeps_cleanup_alive_during_shutdown(self):
        service = SERVICE_TEMPLATE.read_text(encoding="utf-8")

        directives = {
            line.partition("=")[0]: line.partition("=")[2]
            for line in service.splitlines()
            if "=" in line
        }
        self.assertEqual(directives.get("TimeoutStartSec"), "0")
        self.assertEqual(directives.get("KillMode"), "mixed")
        self.assertGreaterEqual(int(directives.get("TimeoutStopSec", "0").removesuffix("min")), 10)
        self.assertEqual(directives.get("PrivateTmp"), "yes")
        self.assertEqual(directives.get("ProtectSystem"), "strict")
        self.assertEqual(directives.get("ProtectHome"), "read-only")
        self.assertEqual(directives.get("ReadOnlyPaths"), "@REPO_ROOT@")
        self.assertEqual(directives.get("ReadWritePaths"), "@STATE_DIR@")

    def test_systemd_templates_use_one_persistent_daily_user_timer_and_credential_paths(self):
        self.assertTrue(SERVICE_TEMPLATE.is_file(), "backup service template is required")
        self.assertTrue(TIMER_TEMPLATE.is_file(), "backup timer template is required")
        service = SERVICE_TEMPLATE.read_text(encoding="utf-8")
        timer = TIMER_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("LoadCredentialEncrypted=rclone-config:", service)
        self.assertIn("LoadCredentialEncrypted=rclone-config-passphrase:", service)
        self.assertIn("PORTAL_BACKUP_STATE_DIR=@STATE_DIR@", service)
        self.assertIn("PORTAL_BACKUP_EVIDENCE=@STATE_DIR@/.portal-backup-verified", service)
        self.assertIn("PORTAL_RCLONE_CONFIG_FILE=%d/rclone-config", service)
        self.assertIn("PORTAL_RCLONE_PASSWORD_COMMAND=/usr/bin/cat %d/rclone-config-passphrase", service)
        self.assertIn("ExecStart=", service)
        self.assertIn("--run", service)
        self.assertIn("ReadOnlyPaths=@REPO_ROOT@", service)
        self.assertIn("ReadWritePaths=@STATE_DIR@", service)
        self.assertNotIn("InaccessiblePaths=/mnt/c", service)
        self.assertIn("OnCalendar=daily", timer)
        self.assertIn("Persistent=true", timer)
        self.assertNotIn("CronJob", service + timer)
        self.assertNotIn("RCLONE_CONFIG_PASS", service + timer)


if __name__ == "__main__":
    unittest.main()
