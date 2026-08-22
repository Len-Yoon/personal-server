from datetime import date
import unittest

from app.models import MaintenanceRecord
from app.services.maintenance import evaluate_maintenance


class MaintenanceRulesTest(unittest.TestCase):
    def test_engine_oil_alert_starts_at_9500km_after_service(self) -> None:
        records = {
            "engine_oil": MaintenanceRecord("engine_oil", 50000, date(2026, 1, 1)),
            "transmission_oil": None,
        }

        alerts = evaluate_maintenance(59500, date(2026, 8, 22), records)

        self.assertEqual(alerts[0].key, "maintenance:engine_oil")
        self.assertIn("500km", alerts[0].text)

    def test_transmission_oil_is_due_at_60000km_after_service(self) -> None:
        record = MaintenanceRecord("transmission_oil", 10000, date(2025, 1, 1))

        alerts = evaluate_maintenance(
            70000,
            date(2026, 8, 22),
            {"engine_oil": None, "transmission_oil": record},
        )

        self.assertEqual(alerts[0].key, "maintenance:transmission_oil")

    def test_engine_oil_time_prealert_starts_30_days_before_due_date(self) -> None:
        record = MaintenanceRecord("engine_oil", 10000, date(2025, 9, 21))

        alerts = evaluate_maintenance(
            10000,
            date(2026, 8, 22),
            {"engine_oil": record, "transmission_oil": None},
        )

        self.assertEqual(alerts[0].key, "maintenance:engine_oil")
        self.assertIn("30일", alerts[0].text)

    def test_transmission_oil_prealert_starts_with_500km_remaining(self) -> None:
        record = MaintenanceRecord("transmission_oil", 0, date(2025, 1, 1))

        alerts = evaluate_maintenance(
            59500,
            date(2026, 8, 22),
            {"engine_oil": None, "transmission_oil": record},
        )

        self.assertEqual(alerts[0].key, "maintenance:transmission_oil")
        self.assertIn("500km", alerts[0].text)


if __name__ == "__main__":
    unittest.main()
