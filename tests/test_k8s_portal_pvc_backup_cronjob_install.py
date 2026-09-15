import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "k8s" / "tools" / "portal-pvc-backup-cronjob.sh"


class PortalPvcBackupCronJobInstallTests(unittest.TestCase):
    def run_tool(self, mode, *, timer="inactive", secret_keys=None, pvc_mode="ReadWriteOnce", configmaps_present=True, configmap_create_race=False):
        secret_keys = secret_keys if secret_keys is not None else {
            "rclone-config",
            "rclone-config-passphrase",
            "age-recipient",
            "age-identity",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            calls = root / "calls.log"
            secret_lines = "\n    ".join(
                f"printf '%s\\n' '{key}: 1 bytes'" for key in sorted(secret_keys)
            )
            self.write_fake(bin_dir / "sudo", f'''#!/bin/sh
printf '%s\\n' "sudo $*" >> "{calls}"
shift
exec "$@"
''')
            self.write_fake(bin_dir / "k3s", f'''#!/bin/sh
printf '%s\\n' "k3s $*" >> "{calls}"
shift
case "$*" in
  *"describe secret portal-pvc-backup-runtime"*)
    printf '%s\\n' "Name: portal-pvc-backup-runtime"
    printf '%s\\n' "Data"
    {secret_lines}
    ;;
  *"get pvc/"*) printf '%s\\n' "Bound {pvc_mode}" ;;
  *"get configmap "*)
    if [ "{str(configmaps_present).lower()}" = true ] || [ -f "{root}/configmap-created" ]; then
      printf '%s\\n' 'configmap/fixed-name'
    fi
    ;;
  *"create configmap "*)
    if [ "{str(configmap_create_race).lower()}" = true ]; then touch "{root}/configmap-created"; exit 42; fi
    ;;
  *) : ;;
esac
''')
            self.write_fake(bin_dir / "systemctl", f'''#!/bin/sh
printf '%s\\n' "systemctl $*" >> "{calls}"
case "$*" in
  *"show personal-server-portal-pvc-backup.timer --property=LoadState --value"*)
    [ "{timer}" != error ] || exit 42
    if [ "{timer}" = missing ]; then printf '%s\\n' not-found; else printf '%s\\n' loaded; fi
    ;;
  *"show personal-server-portal-pvc-backup.timer --property=ActiveState --value"*)
    [ "{timer}" != error ] || exit 42
    if [ "{timer}" = active ]; then printf '%s\\n' active; else printf '%s\\n' inactive; fi
    ;;
esac
''')
            env = {
                **os.environ,
                "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                "PORTAL_BACKUP_KUBECTL": "sudo -n k3s kubectl",
                "PORTAL_BACKUP_LEGACY_TIMER": "personal-server-portal-pvc-backup.timer",
            }
            result = subprocess.run(["bash", str(SCRIPT), mode], env=env, text=True, capture_output=True)
            return result, calls.read_text(encoding="utf-8") if calls.exists() else ""

    @staticmethod
    def write_fake(path, text):
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def test_preflight_checks_only_secret_key_names_and_rwo_pvc_contract(self):
        result, calls = self.run_tool("--preflight")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("portal_pvc_backup_cronjob_preflight=PASS", result.stdout)
        self.assertIn("describe secret portal-pvc-backup-runtime", calls)
        self.assertIn("get pvc/portal-web-files-dynamic", calls)
        self.assertNotIn("get secret portal-pvc-backup-runtime -o", calls)

    def test_activate_refuses_when_legacy_timer_is_active(self):
        result, calls = self.run_tool("--activate", timer="active")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("show personal-server-portal-pvc-backup.timer --property=ActiveState --value", calls)
        self.assertNotIn("patch cronjob portal-pvc-backup", calls)

    def test_activate_refuses_when_legacy_timer_state_cannot_be_read(self):
        result, calls = self.run_tool("--activate", timer="error")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("show personal-server-portal-pvc-backup.timer --property=LoadState --value", calls)
        self.assertNotIn("patch cronjob portal-pvc-backup", calls)

    def test_activate_refuses_when_backup_secret_key_contract_is_missing(self):
        result, calls = self.run_tool("--activate", secret_keys=set())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("describe secret portal-pvc-backup-runtime", calls)
        self.assertNotIn("patch cronjob portal-pvc-backup", calls)

    def test_apply_keeps_cronjob_suspended_and_activate_uses_fixed_patch(self):
        applied, apply_calls = self.run_tool("--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn("apply -f", apply_calls)
        activated, activate_calls = self.run_tool("--activate")
        self.assertEqual(activated.returncode, 0, activated.stderr)
        self.assertIn("patch cronjob portal-pvc-backup --type merge -p {\"spec\":{\"suspend\":false}}", activate_calls)

    def test_apply_creates_missing_nonsecret_status_configmaps_without_overwriting_existing_ones(self):
        missing, missing_calls = self.run_tool("--apply", configmaps_present=False)
        self.assertEqual(missing.returncode, 0, missing.stderr)
        self.assertIn("create configmap portal-pvc-backup-evidence --from-literal=evidence=", missing_calls)
        self.assertIn("create configmap sre-telegram-backup-status --from-literal=run_id=", missing_calls)
        existing, existing_calls = self.run_tool("--apply", configmaps_present=True)
        self.assertEqual(existing.returncode, 0, existing.stderr)
        self.assertNotIn("create configmap portal-pvc-backup-evidence", existing_calls)
        self.assertNotIn("create configmap sre-telegram-backup-status", existing_calls)

    def test_apply_accepts_configmap_create_race_only_after_the_object_is_reconfirmed(self):
        result, calls = self.run_tool("--apply", configmaps_present=False, configmap_create_race=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("create configmap portal-pvc-backup-evidence", calls)
        self.assertGreaterEqual(calls.count("get configmap portal-pvc-backup-evidence"), 2)


if __name__ == "__main__":
    unittest.main()
