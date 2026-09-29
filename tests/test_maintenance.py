import os
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]


class MaintenanceTests(unittest.TestCase):
    def test_host_news_prune_commands_preserve_crawler_archive(self):
        with TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "news_archive.json"
            original = json.dumps(
                {"articles": [{"url": "https://example.com/old", "collected_at": "2020-01-01T00:00:00Z"}]}
            )
            archive_path.write_text(original, encoding="utf-8")
            original_mtime = archive_path.stat().st_mtime_ns
            env = dict(
                os.environ,
                DATA_ROOT=temp_dir,
                BACKUP_PATH=str(Path(temp_dir) / "backups"),
                NEWS_ARCHIVE_PATH=str(archive_path),
                NEWS_RETENTION_DAYS="7",
                SECURITY_LOG_PATH=str(Path(temp_dir) / "logs" / "security-events.txt"),
            )
            for command in ("prune-news", "all"):
                with self.subTest(command=command):
                    result = subprocess.run(
                        [sys.executable, str(ROOT / "scripts" / "maintenance.py"), command],
                        env=env,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(archive_path.read_text(encoding="utf-8"), original)
                    self.assertEqual(archive_path.stat().st_mtime_ns, original_mtime)

    def test_daily_maintenance_still_runs_backup_and_log_cleanup(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_dir = root / "logs"
            log_dir.mkdir()
            old_log = log_dir / "security-events-old.txt"
            old_log.write_text("old", encoding="utf-8")
            os.utime(old_log, (0, 0))
            backup_root = root / "backups"
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "maintenance.py"), "all"],
                env=dict(
                    os.environ,
                    DATA_ROOT=temp_dir,
                    BACKUP_PATH=str(backup_root),
                    SECURITY_LOG_PATH=str(log_dir / "security-events.txt"),
                ),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(any(backup_root.iterdir()))
            self.assertFalse(old_log.exists())

if __name__ == "__main__":
    unittest.main()
