import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "k8s" / "tools" / "quarterly-sre-audit-automation.sh"


class QuarterlySreAuditAutomationTests(unittest.TestCase):
    def run_tool(self, mode, *, timer="loaded", manual_job="success", status="success"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            calls = root / "calls.log"

            def write_fake(name, body):
                path = bin_dir / name
                path.write_text(body, encoding="utf-8")
                path.chmod(0o755)

            write_fake(
                "sudo",
                f'''#!/bin/sh
printf '%s\\n' "sudo $*" >> "{calls}"
[ "$1" = -n ] && shift
exec "$@"
''',
            )
            write_fake(
                "k3s",
                f'''#!/bin/sh
printf '%s\\n' "k3s $*" >> "{calls}"
case "$*" in
  *"get nodes"*) printf '%s\\n' 'node Ready worker' ;;
  *"get cronjob quarterly-sre-audit"*"jsonpath={{.spec.suspend}}"*) printf '%s' true ;;
  *"wait --for=condition=complete job/quarterly-sre-audit-manual-"*)
    [ "{manual_job}" = success ] || exit 42
    ;;
  *"get job quarterly-sre-audit-manual-"*"jsonpath={{.status.succeeded}}"*) printf '%s' 1 ;;
  *"get configmap sre-telegram-quarterly-audit-status"*)
    [ "{status}" != read_failure ] || exit 42
    if [ "{status}" = reported_failure ]; then printf '%s' failed; else printf '%s' passed; fi
    ;;
esac
''',
            )
            write_fake(
                "systemctl",
                f'''#!/bin/sh
printf '%s\\n' "systemctl $*" >> "{calls}"
case "$*" in
  *"show personal-server-quarterly-sre-audit.timer --property=LoadState --value"*)
    if [ "{timer}" = missing ]; then printf '%s\\n' not-found; else printf '%s\\n' loaded; fi
    ;;
  *"show personal-server-quarterly-sre-audit.timer --property=ActiveState --value"*)
    printf '%s\\n' inactive
    ;;
esac
''',
            )
            result = subprocess.run(
                ["bash", str(SCRIPT), mode],
                env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "QUARTERLY_SRE_AUDIT_LEGACY_TIMER": "personal-server-quarterly-sre-audit.timer"},
                text=True,
                capture_output=True,
                check=False,
            )
            return result, calls.read_text(encoding="utf-8") if calls.exists() else ""

    def test_install_applies_suspended_cronjob_disables_legacy_timer_and_activates_after_manual_job(self):
        result, calls = self.run_tool("--install")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("quarterly_sre_audit_install=PASS", result.stdout)
        apply_at = calls.index("apply -f")
        suspended_at = calls.index("get cronjob quarterly-sre-audit -o jsonpath={.spec.suspend}")
        disable_at = calls.index("disable --now personal-server-quarterly-sre-audit.timer")
        create_at = calls.index("create job quarterly-sre-audit-manual-")
        wait_at = calls.index("wait --for=condition=complete job/quarterly-sre-audit-manual-")
        status_at = calls.index("get configmap sre-telegram-quarterly-audit-status")
        activate_at = calls.index("patch cronjob quarterly-sre-audit")
        self.assertLess(apply_at, suspended_at)
        self.assertLess(suspended_at, disable_at)
        self.assertLess(disable_at, create_at)
        self.assertLess(create_at, wait_at)
        self.assertLess(wait_at, status_at)
        self.assertLess(status_at, activate_at)

    def test_install_skips_legacy_timer_disable_when_unit_is_absent(self):
        result, calls = self.run_tool("--install", timer="missing")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("show personal-server-quarterly-sre-audit.timer --property=LoadState --value", calls)
        self.assertNotIn("disable --now personal-server-quarterly-sre-audit.timer", calls)

    def test_install_leaves_cronjob_suspended_when_manual_job_fails(self):
        result, calls = self.run_tool("--install", manual_job="failure")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wait --for=condition=complete job/quarterly-sre-audit-manual-", calls)
        self.assertNotIn("patch cronjob quarterly-sre-audit", calls)

    def test_install_leaves_cronjob_suspended_when_status_reporting_cannot_be_read(self):
        result, calls = self.run_tool("--install", status="read_failure")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wait --for=condition=complete job/quarterly-sre-audit-manual-", calls)
        self.assertIn("get configmap sre-telegram-quarterly-audit-status", calls)
        self.assertNotIn("patch cronjob quarterly-sre-audit", calls)

    def test_install_leaves_cronjob_suspended_when_manual_job_reports_failed_audit_status(self):
        result, calls = self.run_tool("--install", status="reported_failure")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("get configmap sre-telegram-quarterly-audit-status", calls)
        self.assertNotIn("patch cronjob quarterly-sre-audit", calls)

    def test_preflight_does_not_read_or_create_secret_values(self):
        result, calls = self.run_tool("--preflight")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("quarterly_sre_audit_preflight=PASS", result.stdout)
        self.assertNotIn("secret", calls.lower())

    def test_status_reads_cronjob_and_reported_audit_state_without_accessing_secrets(self):
        result, calls = self.run_tool("--status")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("get cronjob quarterly-sre-audit", calls)
        self.assertIn("get configmap sre-telegram-quarterly-audit-status", calls)
        self.assertNotIn("secret", calls.lower())


if __name__ == "__main__":
    unittest.main()
