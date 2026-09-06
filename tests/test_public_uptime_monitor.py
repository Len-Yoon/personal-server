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
            'if (state === "success" && incident) {\n'
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

    def test_ci_runs_the_uptime_monitor_contract(self):
        ci_workflow = CI_WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("tests.test_public_uptime_monitor", ci_workflow)
