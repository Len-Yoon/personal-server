import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "infra/k8s/tools/monthly-recovery-drill.sh"


class MonthlyRecoveryDrillTests(unittest.TestCase):
    def run_drill(self, scripts, *, fail_stage=None, preexisting=False):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log = tmp_path / "calls.log"
            tools = {}
            for name, body in scripts.items():
                tool = tmp_path / name
                tool.write_text(
                    "#!/usr/bin/env bash\nset -u\n"
                    f"printf '%s %s\\n' {name!r} \"$*\" >> \"$CALL_LOG\"\n"
                    + body,
                    encoding="utf-8",
                )
                tool.chmod(0o700)
                tools[name] = tool
            env = os.environ.copy()
            env.update(
                {
                    "CALL_LOG": str(log),
                    "RECOVERY_DRILL_STATE_DIR": str(tmp_path / "evidence"),
                    "RECOVERY_DRILL_RUN_ID": "20260911T120000Z-test",
                    "RECOVERY_DRILL_POD_RUN_ID": "lab-test",
                    "MONTHLY_RECOVERY_DRILL_BACKUP_TOOL": str(tools["backup"]),
                    "MONTHLY_RECOVERY_DRILL_TELEGRAM_TOOL": str(tools["telegram"]),
                    "MONTHLY_RECOVERY_DRILL_POD_TOOL": str(tools["pod"]),
                }
            )
            if preexisting:
                existing = tmp_path / "evidence" / "20260911T120000Z-test.json"
                existing.parent.mkdir(parents=True, exist_ok=True)
                existing.write_text('{"status":"original"}\n', encoding="utf-8")
            if fail_stage:
                env["FAIL_STAGE"] = fail_stage
            result = subprocess.run(
                [str(RUNNER)], env=env, text=True, capture_output=True
            )
            evidence = tmp_path / "evidence" / "20260911T120000Z-test.json"
            log_text = log.read_text(encoding="utf-8") if log.exists() else ""
            evidence_text = evidence.read_text(encoding="utf-8") if evidence.exists() else ""
            return result, log_text, evidence_text

    def test_success_runs_three_steps_in_order_and_cleans_up_lab(self):
        scripts = {
            "backup": "[[ ${1:-} == --check ]]\n",
            "telegram": "",
            "pod": "if [[ ${1:-} == --run ]]; then echo sre_pod_recovery_run_id=lab-test; fi\n",
        }
        result, log, evidence = self.run_drill(scripts)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            log.splitlines(),
            [
                "backup --check",
                "telegram ",
                "pod --run",
                "pod --cleanup lab-test",
            ],
        )
        payload = json.loads(evidence)
        self.assertEqual(payload["status"], "success")
        self.assertIsNone(payload["failed_stage"])
        self.assertEqual([stage["name"] for stage in payload["stages"]], ["backup", "telegram", "pod"])
        self.assertTrue(payload["stages"][-1]["cleanup"])

    def test_failed_pod_still_requests_cleanup_with_preassigned_safe_run_id(self):
        scripts = {
            "backup": "",
            "telegram": "",
            "pod": "if [[ ${1:-} == --run ]]; then exit 1; fi\n",
        }
        result, log, evidence = self.run_drill(scripts)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(log.splitlines(), ["backup --check", "telegram ", "pod --run", "pod --cleanup lab-test"])
        payload = json.loads(evidence)
        self.assertEqual(payload["failed_stage"], "pod")
        self.assertEqual(payload["pod_run_id"], "lab-test")
        self.assertTrue(payload["stages"][-1]["cleanup"])

    def test_existing_evidence_is_not_overwritten(self):
        scripts = {"backup": "", "telegram": "", "pod": "echo sre_pod_recovery_run_id=lab-test\n"}
        result, _log, content = self.run_drill(scripts, preexisting=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(content, '{"status":"original"}\n')

    def test_failure_stops_following_steps_and_records_failed_stage(self):
        scripts = {
            "backup": "[[ ${1:-} == --check ]]\n",
            "telegram": "[[ ${FAIL_STAGE:-} != telegram ]]\n",
            "pod": "if [[ ${1:-} == --run ]]; then echo sre_pod_recovery_run_id=lab-test; fi\n",
        }
        result, log, evidence = self.run_drill(scripts, fail_stage="telegram")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(log.splitlines(), ["backup --check", "telegram "])
        payload = json.loads(evidence)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["failed_stage"], "telegram")

    def test_evidence_does_not_contain_command_output_or_secrets(self):
        scripts = {
            "backup": "echo TELEGRAM_BOT_TOKEN=do-not-record\n",
            "telegram": "",
            "pod": "if [[ ${1:-} == --run ]]; then echo sre_pod_recovery_run_id=lab-test; fi\n",
        }
        result, _log, evidence = self.run_drill(scripts)
        self.assertEqual(result.returncode, 0, result.stderr)
        content = evidence
        self.assertNotIn("TELEGRAM_BOT_TOKEN", content)
        self.assertNotIn("do-not-record", content)


if __name__ == "__main__":
    unittest.main()
