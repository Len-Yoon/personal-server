import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "k8s" / "tools" / "quarterly-sre-audit-automation.sh"


class QuarterlySreAuditAutomationTests(unittest.TestCase):
    def run_tool(
        self,
        mode,
        *,
        timer="loaded",
        legacy_service="inactive",
        manual_job="success",
        status="success",
        configmap="existing",
        active_jobs="none",
        active_jobs_after_preflight=False,
        lock="available",
        manifest_crlf=False,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            calls = root / "calls.log"
            jobs_query_count = root / "jobs-query-count"
            manifest = root / "quarterly-sre-audit-cronjob.yaml"
            manifest.write_bytes(
                (ROOT / "infra" / "k8s" / "sre-audit-automation" / "quarterly-sre-audit-cronjob.yaml")
                .read_bytes()
                .replace(b"\n", b"\r\n")
                if manifest_crlf
                else (ROOT / "infra" / "k8s" / "sre-audit-automation" / "quarterly-sre-audit-cronjob.yaml").read_bytes()
            )

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
  *"get jobs -o jsonpath="*)
    [ "{active_jobs}" != error ] || exit 42
    count=$(cat "{jobs_query_count}" 2>/dev/null || printf 0)
    count=$((count + 1))
    printf '%s' "$count" > "{jobs_query_count}"
    case "{active_jobs}" in
      active|active_empty|active_zero|terminal_complete_empty|terminal_complete_zero|terminal_failed_empty|terminal_failed_zero)
        printf '%s\\n' quarterly-sre-audit-manual-existing
        ;;
    esac
    if [ "{str(active_jobs_after_preflight).lower()}" = true ] && [ "$count" -gt 1 ]; then
      printf '%s\\n' quarterly-sre-audit-manual-existing
    fi
    ;;
  *"get job quarterly-sre-audit-manual-existing"*"jsonpath={{range .status.conditions"*)
    case "{active_jobs}" in
      terminal_complete_empty|terminal_complete_zero) printf '%s' Complete=True, ;;
      terminal_failed_empty|terminal_failed_zero) printf '%s' Failed=True, ;;
    esac
    ;;
  *"get cronjob quarterly-sre-audit"*"jsonpath={{.spec.suspend}}"*) printf '%s' true ;;
  *"wait --for=condition=complete job/quarterly-sre-audit-manual-"*)
    [ "{manual_job}" = success ] || exit 42
    ;;
  *"get job quarterly-sre-audit-manual-"*"jsonpath={{.status.succeeded}}"*) printf '%s' 1 ;;
  *"get configmap sre-telegram-quarterly-audit-status --ignore-not-found -o name"*)
    if [ "{configmap}" = existing ] || [ -f "{root}/configmap-created" ]; then printf '%s\\n' configmap/sre-telegram-quarterly-audit-status; fi
    ;;
  *"create configmap sre-telegram-quarterly-audit-status"*) touch "{root}/configmap-created" ;;
  *"get configmap sre-telegram-quarterly-audit-status"*"jsonpath={{.data.run_id}}"*)
    [ "{status}" != status_detail_read_failure ] || exit 42
    printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' run-123 passed 2026-09-12T01:02:03Z passed passed passed
    ;;
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
  *"show personal-server-quarterly-sre-audit.service --property=LoadState --value"*)
    if [ "{legacy_service}" = missing ]; then printf '%s\\n' not-found; else printf '%s\\n' loaded; fi
    ;;
  *"show personal-server-quarterly-sre-audit.service --property=ActiveState --value"*)
    printf '%s\\n' '{legacy_service}'
    ;;
esac
''',
            )
            write_fake(
                "flock",
                f'''#!/bin/sh
[ "{lock}" != busy ] || exit 42
exit 0
''',
            )
            if manifest_crlf:
                write_fake(
                    "grep",
                    '''#!/bin/sh
case "$*" in
  *'\\r'*) exit 1 ;;
esac
exec /usr/bin/grep "$@"
''',
                )
            result = subprocess.run(
                ["bash", str(SCRIPT), mode],
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "QUARTERLY_SRE_AUDIT_LEGACY_TIMER": "personal-server-quarterly-sre-audit.timer",
                    "QUARTERLY_SRE_AUDIT_MANIFEST": str(manifest) if manifest_crlf else os.environ.get("QUARTERLY_SRE_AUDIT_MANIFEST", ""),
                },
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
        status_at = calls.rindex("get configmap sre-telegram-quarterly-audit-status -o jsonpath={.data.status}")
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

    def test_install_creates_empty_status_configmap_only_when_it_is_missing(self):
        result, calls = self.run_tool("--install", configmap="missing")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("get configmap sre-telegram-quarterly-audit-status --ignore-not-found -o name", calls)
        self.assertIn("create configmap sre-telegram-quarterly-audit-status --from-literal=run_id=", calls)
        self.assertNotIn("secret", calls.lower())

    def test_install_preserves_existing_status_configmap_data(self):
        result, calls = self.run_tool("--install", configmap="existing")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("get configmap sre-telegram-quarterly-audit-status --ignore-not-found -o name", calls)
        self.assertNotIn("create configmap sre-telegram-quarterly-audit-status", calls)

    def test_install_lock_contention_blocks_before_manifest_apply(self):
        result, calls = self.run_tool("--install", lock="busy")

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("apply -f", calls)
        self.assertNotIn("create job quarterly-sre-audit-manual-", calls)

    def test_active_quarterly_job_blocks_before_manifest_apply(self):
        result, calls = self.run_tool("--install", active_jobs="active")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("get jobs -o jsonpath=", calls)
        self.assertNotIn("apply -f", calls)
        self.assertNotIn("create job quarterly-sre-audit-manual-", calls)

    def test_matching_job_with_empty_active_and_no_terminal_condition_blocks_install(self):
        result, calls = self.run_tool("--install", active_jobs="active_empty")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("get jobs -o jsonpath=", calls)
        self.assertNotIn("apply -f", calls)
        self.assertNotIn("create job quarterly-sre-audit-manual-", calls)

    def test_matching_job_with_zero_active_and_no_terminal_condition_blocks_install(self):
        result, calls = self.run_tool("--install", active_jobs="active_zero")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("get jobs -o jsonpath=", calls)
        self.assertNotIn("apply -f", calls)
        self.assertNotIn("create job quarterly-sre-audit-manual-", calls)

    def test_complete_terminal_job_with_missing_active_allows_install(self):
        result, calls = self.run_tool("--install", active_jobs="terminal_complete_empty")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("get job quarterly-sre-audit-manual-existing", calls)
        self.assertIn("create job quarterly-sre-audit-manual-", calls)

    def test_complete_terminal_job_with_zero_active_allows_install(self):
        result, calls = self.run_tool("--install", active_jobs="terminal_complete_zero")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("get job quarterly-sre-audit-manual-existing", calls)
        self.assertIn("create job quarterly-sre-audit-manual-", calls)

    def test_failed_terminal_job_with_missing_active_allows_install(self):
        result, calls = self.run_tool("--install", active_jobs="terminal_failed_empty")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("get job quarterly-sre-audit-manual-existing", calls)
        self.assertIn("create job quarterly-sre-audit-manual-", calls)

    def test_failed_terminal_job_with_zero_active_allows_install(self):
        result, calls = self.run_tool("--install", active_jobs="terminal_failed_zero")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("get job quarterly-sre-audit-manual-existing", calls)
        self.assertIn("create job quarterly-sre-audit-manual-", calls)

    def test_job_query_error_blocks_before_manifest_apply(self):
        result, calls = self.run_tool("--install", active_jobs="error")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("get jobs -o jsonpath=", calls)
        self.assertNotIn("apply -f", calls)

    def test_retry_does_not_start_manual_job_when_another_job_appears_after_preflight(self):
        result, calls = self.run_tool("--install", active_jobs_after_preflight=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertGreaterEqual(calls.count("get jobs -o jsonpath="), 2)
        self.assertNotIn("create job quarterly-sre-audit-manual-", calls)
        self.assertNotIn("patch cronjob quarterly-sre-audit", calls)

    def test_active_legacy_service_blocks_without_forcing_service_stop(self):
        result, calls = self.run_tool("--install", legacy_service="active")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("disable --now personal-server-quarterly-sre-audit.timer", calls)
        self.assertIn("show personal-server-quarterly-sre-audit.service --property=ActiveState --value", calls)
        self.assertNotIn("stop personal-server-quarterly-sre-audit.service", calls)
        self.assertNotIn("create job quarterly-sre-audit-manual-", calls)

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

    def test_preflight_accepts_suspended_cronjob_manifest_with_crlf_line_endings(self):
        result, calls = self.run_tool("--preflight", manifest_crlf=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("quarterly_sre_audit_preflight=PASS", result.stdout)
        self.assertIn("apply --dry-run=client -f", calls)

    def test_status_reports_allowed_audit_fields_without_accessing_secrets(self):
        result, calls = self.run_tool("--status")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cronjob_suspended=true", result.stdout)
        self.assertIn("run_id=run-123", result.stdout)
        self.assertIn("status=passed", result.stdout)
        self.assertIn("completed_at=2026-09-12 10:02", result.stdout)
        self.assertNotIn("completed_at=2026-09-12T01:02:03Z", result.stdout)
        self.assertIn("health_audit=passed", result.stdout)
        self.assertIn("backup_check=passed", result.stdout)
        self.assertIn("recovery_lab=passed", result.stdout)
        self.assertIn("get cronjob quarterly-sre-audit -o jsonpath=", calls)
        self.assertIn("get configmap sre-telegram-quarterly-audit-status", calls)
        self.assertNotIn("secret", calls.lower())

    def test_status_fails_closed_when_audit_status_configmap_cannot_be_read(self):
        result, _ = self.run_tool("--status", status="status_detail_read_failure")

        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
