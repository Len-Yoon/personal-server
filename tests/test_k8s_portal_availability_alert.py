from pathlib import Path
import unittest


RULES = (
    Path(__file__).resolve().parents[1]
    / "infra"
    / "k8s"
    / "sre-telegram"
    / "prometheus-rule.yaml"
)


class PortalAvailabilityAlertContractTest(unittest.TestCase):
    def test_portal_alert_requires_an_expected_k3s_replica_and_two_minute_outage(self):
        text = RULES.read_text(encoding="utf-8")

        self.assertIn("- alert: PortalUnavailable", text)
        self.assertIn(
            'kube_deployment_spec_replicas{namespace="personal-server",deployment="portal-web"} > 0',
            text,
        )
        self.assertIn(
            'kube_deployment_status_replicas_available{namespace="personal-server",deployment="portal-web"} == 0',
            text,
        )
        self.assertIn("for: 2m", text)
        self.assertIn('severity: critical', text)
        self.assertIn('sre_telegram: "true"', text)
        self.assertIn('summary: "Portal 미가용"', text)

    def test_generic_deployment_alert_excludes_portal_to_prevent_duplicate_notifications(self):
        text = RULES.read_text(encoding="utf-8")

        self.assertIn('deployment!="portal-web"', text)

    def test_portal_http_alerts_notify_after_sustained_error_or_latency(self):
        text = RULES.read_text(encoding="utf-8")

        for alert, summary in (
            ("PortalHttp5xxErrorRateHigh", "Portal HTTP 5xx 오류율 높음"),
            ("PortalHttpP95LatencyHigh", "Portal HTTP p95 응답 지연"),
        ):
            self.assertIn(f"- alert: {alert}", text)
            self.assertIn("for: 5m", text)
            self.assertIn('sre_telegram: "true"', text)
            self.assertIn(f'summary: "{summary}"', text)

        self.assertIn('portal_http_requests_total{status_code=~"5.."}', text)
        self.assertIn('sum(rate(portal_http_requests_total[5m])) > 0.1', text)
        self.assertIn('portal_http_request_duration_seconds_bucket[5m]', text)


if __name__ == "__main__":
    unittest.main()
