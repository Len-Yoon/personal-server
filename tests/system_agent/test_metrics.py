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
        self.assertIn("status_checks", metrics)
        self.assertTrue(all(check["status"] == "ok" for check in metrics["status_checks"]))

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
            self.assertEqual(metrics["host"]["status"], "warning")
            self.assertTrue(any(check["key"] == "host" and check["status"] == "warning" for check in metrics["status_checks"]))

    def test_collect_metrics_accepts_utf8_bom_host_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("system-agent")
            root = Path(tempdir)
            host_file = root / "system" / "host-metrics.json"
            host_file.parent.mkdir(parents=True)
            host_file.write_bytes(
                b"\xef\xbb\xbf"
                + json.dumps(
                    {
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                        "cpu_percent": 21.5,
                        "memory_percent": 37.2,
                        "disk_percent": 48.1,
                        "uptime_seconds": 5432,
                    }
                ).encode("utf-8")
            )

            from app.services.metrics import collect_metrics

            metrics = collect_metrics(
                data_root=root,
                host_metrics_path=host_file,
                stale_after_seconds=300,
            )

            self.assertEqual(metrics["host"]["source"], "windows")
            self.assertEqual(metrics["host"]["cpu_percent"], 21.5)

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

    def test_collect_metrics_marks_critical_disk_usage(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("system-agent")
            root = Path(tempdir)
            (root / "system").mkdir(parents=True, exist_ok=True)
            host_file = root / "system" / "host-metrics.json"
            host_file.write_text(
                json.dumps(
                    {
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                        "cpu_percent": 10.0,
                        "memory_percent": 20.0,
                        "disk_percent": 30.0,
                        "uptime_seconds": 123,
                    }
                ),
                encoding="utf-8",
            )

            from app.services import metrics as metrics_module

            original_disk_usage = metrics_module.shutil.disk_usage

            class FakeUsage:
                total = 100
                used = 95
                free = 5

            try:
                metrics_module.shutil.disk_usage = lambda target: FakeUsage()
                metrics = metrics_module.collect_metrics(
                    data_root=root,
                    host_metrics_path=host_file,
                    stale_after_seconds=300,
                )
            finally:
                metrics_module.shutil.disk_usage = original_disk_usage

            self.assertEqual(metrics["disk"]["level"], "critical")
            self.assertEqual(metrics["overall_status"], "critical")
            self.assertTrue(any(check["key"] == "disk" and check["status"] == "critical" for check in metrics["status_checks"]))


if __name__ == "__main__":
    unittest.main()
