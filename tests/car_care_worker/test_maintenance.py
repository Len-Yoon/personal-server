from datetime import date
import unittest

from app.models import MaintenanceRecord
from app.services.maintenance import evaluate_maintenance


class MaintenanceRulesTest(unittest.TestCase):
    def test_engine_oil_alert_starts_at_9000km_after_service(self) -> None:
        records = {
            "engine_oil": MaintenanceRecord("engine_oil", 50000, date(2026, 1, 1)),
            "transmission_oil": None,
        }

        alerts = evaluate_maintenance(59000, date(2026, 8, 22), records)

        self.assertEqual(alerts[0].key, "maintenance:engine_oil")
        self.assertIn("1,000km", alerts[0].text)

    def test_transmission_oil_is_due_at_60000km_after_service(self) -> None:
        record = MaintenanceRecord("transmission_oil", 10000, date(2025, 1, 1))

        alerts = evaluate_maintenance(
            70000,
            date(2026, 8, 22),
            {"engine_oil": None, "transmission_oil": record},
        )

        self.assertEqual(alerts[0].key, "maintenance:transmission_oil")


if __name__ == "__main__":
    unittest.main()
