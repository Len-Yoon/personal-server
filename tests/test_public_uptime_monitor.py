import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "public-uptime-monitor.yml"
MONITOR_PATH_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "public-uptime-monitor-path.yml"
CI_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
CI_TEST_MATRIX_PATH = ROOT / "tests" / "ci_test_matrix.json"
PUBLIC_HEALTH_TARGETS = {
    "portal": "https://len.pe.kr/health",
    "news": "https://news.len.pe.kr/health",
    "youtube_memo": "https://memo.len.pe.kr/health",
    "book_memo": "https://books.len.pe.kr/health",
}


class PublicUptimeMonitorTests(unittest.TestCase):
    def test_workflow_checks_the_public_health_endpoint_on_a_schedule(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("schedule:", workflow)
        self.assertIn("*/5 * * * *", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("https://len.pe.kr/health", workflow)
        self.assertIn("--max-time", workflow)

    def test_workflow_aggregates_all_fixed_public_service_health_checks(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        for identifier, url in PUBLIC_HEALTH_TARGETS.items():
            self.assertIn(f'check_health "{identifier}" "{url}"', workflow)
        self.assertIn('failed_services+=("$service")', workflow)
        self.assertIn('(IFS=,; printf \'failed_services=%s\\n\' "${failed_services[*]}")', workflow)
        self.assertIn('if ((${#failed_services[@]} > 0)); then', workflow)
        self.assertIn('const failedServices = "${{ steps.health.outputs.failed_services }}";', workflow)
        self.assertIn('body: `외부 건강 점검에서 실패한 서비스: ${failedServices}.`,', workflow)

    def test_workflow_recovers_only_after_all_public_service_health_checks_succeed(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        health_step = workflow[workflow.index("- id: health"):workflow.index("- id: incident")]
        self.assertIn('if ((${#failed_services[@]} > 0)); then', health_step)
        self.assertIn("exit 1", health_step)
        self.assertNotIn("exit 1\n          done", health_step)

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
        self.assertIn('const delivery = "${{ steps.telegram.outputs.delivery }}";', workflow)
        self.assertIn('if (delivery !== "accepted") {', workflow)
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

    def test_source_workflow_records_secret_or_delivery_failure_on_the_public_incident(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn('const deliveryFailureMarker = "<!-- uptime-telegram-delivery:failed -->";', workflow)
        self.assertIn("delivery=unavailable", workflow)
        self.assertIn("delivery=failed", workflow)
        self.assertIn("if: always() && steps.incident.outputs.notification != ''", workflow)
        self.assertIn('if (delivery === "failed" || delivery === "unavailable") {', workflow)
        self.assertIn("알림 미전송: 상태 전환 Telegram 전송 증적이 없음", workflow)
        self.assertIn("steps.telegram.outputs.delivery", workflow)

    def test_monitor_path_workflow_observes_only_completed_uptime_runs_with_minimum_permissions(self):
        workflow = MONITOR_PATH_WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("workflow_run:", workflow)
        self.assertIn('workflows: ["Public Portal Uptime Monitor"]', workflow)
        self.assertIn("types: [completed]", workflow)
        self.assertIn("actions: read", workflow)
        self.assertIn("issues: write", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn("actions/checkout", workflow)
        self.assertNotIn("download-artifact", workflow)
        self.assertNotIn("github.event.workflow_run.head", workflow)

    def test_monitor_path_workflow_classifies_only_fixed_monitor_job_and_telegram_step(self):
        workflow = MONITOR_PATH_WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("github.rest.actions.listJobsForWorkflowRun", workflow)
        self.assertIn('if (runConclusion !== "failure") {', workflow)
        self.assertIn('job.name === "monitor"', workflow)
        self.assertIn('step.name === "Send Telegram status transition"', workflow)
        self.assertIn("const sourceDeliveryFailed =", workflow)
        self.assertIn('telegramStep?.conclusion === "failure"', workflow)
        self.assertNotIn("workflow_run.display_title", workflow)
        self.assertNotIn("workflow_run.head_commit", workflow)
        self.assertNotIn("workflow_run.path", workflow)

    def test_monitor_path_workflow_records_delivery_and_execution_failure_separately(self):
        workflow = MONITOR_PATH_WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn('const incidentTitle = "[SRE] 공개 감시 경로 장애";', workflow)
        self.assertIn('<!-- uptime-monitor-path:execution-failure:sent -->', workflow)
        self.assertIn('<!-- uptime-monitor-path:telegram-delivery-failure:sent -->', workflow)
        self.assertIn('<!-- uptime-monitor-path:recovered:accepted -->', workflow)
        self.assertIn("[감시 실행 실패]", workflow)
        self.assertIn("[감시 알림 전송 실패]", workflow)
        self.assertIn("[감시 경로 복구]", workflow)
        self.assertIn("state: \"closed\"", workflow)

    def test_monitor_path_workflow_closes_unnotified_path_issue_without_false_recovery(self):
        workflow = MONITOR_PATH_WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn('const sourceIncidentTitle = "[SRE] len.pe.kr 공개 상태 장애";', workflow)
        self.assertIn('const sourceDeliveryFailureMarker = "<!-- uptime-telegram-delivery:failed -->";', workflow)
        self.assertIn('const sourceObserverUnavailableMarker = "<!-- uptime-telegram-delivery:observer-unavailable -->";', workflow)
        self.assertIn('const notificationUnavailableMarker = "<!-- uptime-monitor-path:notification-unavailable -->";', workflow)
        self.assertIn("if: always() && steps.incident.outputs.notification != ''", workflow)
        self.assertIn("알림 미전송: 감시 경로 Telegram 전송 증적이 없어 복구 알림을 생략함", workflow)
        self.assertIn('if (!accepted) {', workflow)
        self.assertIn("source_issue_number", workflow)

    def test_monitor_path_workflow_uses_existing_secrets_without_recording_delivery_response(self):
        workflow = MONITOR_PATH_WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("secrets.UPTIME_TELEGRAM_BOT_TOKEN", workflow)
        self.assertIn("secrets.UPTIME_TELEGRAM_CHAT_ID", workflow)
        self.assertIn("jq -e '.ok == true and (.result.message_id | numbers)'", workflow)
        self.assertIn('--output "$telegram_response"', workflow)
        self.assertNotIn("response_body", workflow)
        self.assertNotIn("telegram_response_contents", workflow)
        self.assertNotIn("UPTIME_TELEGRAM_BOT_TOKEN=", workflow)

    def test_ci_runs_the_uptime_monitor_contract(self):
        ci_workflow = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
        matrix = json.loads(CI_TEST_MATRIX_PATH.read_text(encoding="utf-8"))
        commands = "\n".join(entry["test_command"] for entry in matrix)

        self.assertIn("tests/run_service_tests.py --github-matrix", ci_workflow)
        self.assertIn("tests.test_public_uptime_monitor", commands)
