import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "public-uptime-monitor.yml"
CI_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"


class PublicUptimeMonitorTests(unittest.TestCase):
    def test_workflow_checks_the_public_health_endpoint_on_a_schedule(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("schedule:", workflow)
        self.assertIn("*/5 * * * *", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("https://len.pe.kr/health", workflow)
        self.assertIn("--max-time", workflow)

    def test_workflow_notifies_only_when_an_incident_opens_or_recovers(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn('const incidentTitle = "[SRE] len.pe.kr 공개 상태 장애";', workflow)
        self.assertIn('const downSentMarker = "<!-- uptime-down-telegram:sent -->";', workflow)
        self.assertIn('issue.user?.login === "github-actions[bot]"', workflow)
        self.assertIn("state: \"open\"", workflow)
        self.assertIn("notification', 'down'", workflow)
        self.assertIn("notification', 'recovered'", workflow)
        self.assertIn("github.rest.issues.create", workflow)
        self.assertIn("github.rest.issues.update", workflow)
        self.assertIn("Confirm Telegram delivery before committing transition", workflow)
        self.assertIn(
            'if (state === "success" && incident && (incident.body || "").includes(downSentMarker)) {\n'
            "              core.setOutput('notification', 'recovered');",
            workflow,
        )

    def test_workflow_keeps_telegram_values_in_github_secrets(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("secrets.UPTIME_TELEGRAM_BOT_TOKEN", workflow)
        self.assertIn("secrets.UPTIME_TELEGRAM_CHAT_ID", workflow)
        self.assertNotIn("914421", workflow)
        self.assertNotIn("telegram_bot_token=", workflow)
        self.assertIn("permissions:\n  issues: write", workflow)

    def test_workflow_bounds_runtime_and_pins_its_new_action_dependency(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("timeout-minutes: 5", workflow)
        self.assertIn(
            "actions/github-script@f28e40c7f34bde8b3046d885e986cb6290c5673b",
            workflow,
        )

    def test_workflow_classifies_telegram_delivery_failures_without_logging_responses(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("--write-out '%{http_code}'", workflow)
        self.assertIn('case "$telegram_status" in', workflow)
        self.assertIn("Telegram rejected chat configuration.", workflow)
        self.assertIn("Telegram rejected bot credentials.", workflow)
        self.assertIn("Telegram blocked bot delivery.", workflow)
        self.assertIn('--output "$telegram_response"', workflow)
        self.assertNotIn("response_body", workflow)
        self.assertNotIn("description", workflow)

    def test_workflow_verifies_telegram_success_and_records_safe_delivery_evidence(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("telegram_response", workflow)
        self.assertIn("jq -e '.ok == true and (.result.message_id | numbers)'", workflow)
        self.assertIn("accepted=true", workflow)
        self.assertIn("steps.telegram.outputs.accepted == 'true'", workflow)
        self.assertIn("복구 Telegram API 요청 수락 확인됨", workflow)
        self.assertIn("<!-- uptime-recovered-telegram:accepted -->", workflow)
        self.assertIn("state: \"closed\"", workflow)
        self.assertNotIn("TELEGRAM_CHAT_ID: ${{ steps.telegram.outputs", workflow)

    def test_health_and_incident_lifecycle_do_not_exit_early_when_telegram_secrets_are_missing(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertNotIn("name: Check notification secrets", workflow)
        health_start = workflow.index("- id: health")
        telegram_start = workflow.index("- id: telegram")
        self.assertLess(health_start, telegram_start)
        telegram_step = workflow[telegram_start:]
        self.assertIn('if [[ -z "$TELEGRAM_BOT_TOKEN" || -z "$TELEGRAM_CHAT_ID" ]]; then', telegram_step)
        self.assertIn("Telegram notification secrets are not configured; skipping delivery.", telegram_step)

    def test_recovery_closes_an_unnotified_incident_with_safe_evidence(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        branch_start = workflow.index(
            'if (state === "success" && incident && !(incident.body || "").includes(downSentMarker)) {'
        )
        branch_end = workflow.index("- id: telegram", branch_start)
        unnotified_recovery_branch = workflow[branch_start:branch_end]
        self.assertIn("github.rest.issues.update", unnotified_recovery_branch)
        self.assertIn("알림 미전송", unnotified_recovery_branch)
        self.assertIn('state: "closed"', unnotified_recovery_branch)

    def test_ci_runs_the_uptime_monitor_contract(self):
        ci_workflow = CI_WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("tests.test_public_uptime_monitor", ci_workflow)
