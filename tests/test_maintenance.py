import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import maintenance  # noqa: E402


class MaintenanceTests(unittest.TestCase):
    def test_prune_obsidian_news_removes_only_expired_date_named_files(self):
        with TemporaryDirectory() as temp_dir:
            news_dir = Path(temp_dir) / "뉴스" / "Investing"
            news_dir.mkdir(parents=True)
            old_file = news_dir / "2026-06-01.md"
            recent_file = news_dir / "2026-07-10.md"
            other_file = news_dir / "notes.md"
            for path in (old_file, recent_file, other_file):
                path.write_text("content", encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "OBSIDIAN_VAULT_PATH": temp_dir,
                    "OBSIDIAN_NEWS_DIR": "뉴스/Investing",
                    "OBSIDIAN_NEWS_RETENTION_DAYS": "30",
                },
                clear=False,
            ), patch.object(maintenance, "datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 7, 11)
                mocked_datetime.strptime.side_effect = datetime.strptime
                removed = maintenance.prune_obsidian_news()

            self.assertEqual(removed, [old_file])
            self.assertFalse(old_file.exists())
            self.assertTrue(recent_file.exists())
            self.assertTrue(other_file.exists())

    def test_prune_obsidian_news_rejects_negative_retention(self):
        with patch.dict(
            os.environ,
            {"OBSIDIAN_NEWS_RETENTION_DAYS": "-1"},
            clear=False,
        ):
            with self.assertRaises(ValueError):
                maintenance.prune_obsidian_news()


if __name__ == "__main__":
    unittest.main()
