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


if __name__ == "__main__":
    unittest.main()
