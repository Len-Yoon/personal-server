import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests._test_support import prepare_service_import


class SystemAgentMetricsTests(unittest.TestCase):
    def test_demo_metrics_are_safe_and_ok(self):
        prepare_service_import("system-agent")
        from app.services.metrics import demo_metrics

        metrics = demo_metrics()

        self.assertTrue(metrics["demo_mode"])
        self.assertEqual(metrics["overall_status"], "ok")
        self.assertEqual(metrics["host"]["source"], "demo")
        self.assertGreater(metrics["host"]["cpu_percent"], 0)

    def test_collect_metrics_marks_stale_windows_host_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("system-agent")
            root = Path(tempdir)
            host_file = root / "system" / "host-metrics.json"
            host_file.parent.mkdir(parents=True)
            old_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
            host_file.write_text(
                json.dumps(
                    {
                        "captured_at": old_timestamp,
                        "cpu_percent": 17.5,
                        "memory_percent": 41.2,
                        "disk_percent": 62.8,
                        "uptime_seconds": 12345,
                    }
                ),
                encoding="utf-8",
            )

            from app.services.metrics import collect_metrics

            metrics = collect_metrics(
                data_root=root,
                host_metrics_path=host_file,
                stale_after_seconds=300,
            )

            self.assertEqual(metrics["host"]["cpu_percent"], 17.5)
            self.assertIn("host_metrics_stale", metrics["warnings"])
            self.assertEqual(metrics["overall_status"], "warning")

    def test_collect_metrics_reports_recent_backup(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("system-agent")
            root = Path(tempdir)
            backup = root / "backups" / "20260701-030000"
            backup.mkdir(parents=True)

            from app.services.metrics import collect_metrics

            metrics = collect_metrics(data_root=root)

            self.assertTrue(metrics["backup"]["exists"])
            self.assertEqual(metrics["backup"]["latest_name"], "20260701-030000")

    def test_collect_metrics_warns_when_latest_backup_is_old(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("system-agent")
            root = Path(tempdir)
            backup = root / "backups" / "20260601-030000"
            backup.mkdir(parents=True)
            old_time = (datetime.now(timezone.utc) - timedelta(days=3)).timestamp()
            os.utime(backup, (old_time, old_time))

            from app.services.metrics import collect_metrics

            metrics = collect_metrics(data_root=root, backup_stale_after_seconds=86400)

            self.assertTrue(metrics["backup"]["exists"])
            self.assertIn("backup_stale", metrics["warnings"])

    def test_collect_metrics_reports_file_usage(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("system-agent")
            root = Path(tempdir)
            files = root / "files"
            files.mkdir(parents=True)
            (files / "memo.txt").write_text("hello", encoding="utf-8")

            from app.services.metrics import collect_metrics

            metrics = collect_metrics(data_root=root)

            self.assertEqual(metrics["files"]["file_count"], 1)
            self.assertGreaterEqual(metrics["files"]["total_bytes"], 5)

    def test_disk_level_thresholds(self):
        prepare_service_import("system-agent")
        from app.services.metrics import disk_level

        self.assertEqual(disk_level(79.9), "ok")
        self.assertEqual(disk_level(80.0), "warning")
        self.assertEqual(disk_level(90.0), "critical")


if __name__ == "__main__":
    unittest.main()
