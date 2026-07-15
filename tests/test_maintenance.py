import os
import json
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
    def test_prune_news_archive_removes_expired_articles(self):
        with TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "news_archive.json"
            archive_path.write_text(
                json.dumps(
                    {
                        "articles": [
                            {
                                "url": "https://example.com/old",
                                "collected_at": "2026-06-01T00:00:00+00:00",
                            },
                            {
                                "url": "https://example.com/recent",
                                "collected_at": "2026-07-10T00:00:00+00:00",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "NEWS_ARCHIVE_PATH": str(archive_path),
                    "NEWS_RETENTION_DAYS": "7",
                },
                clear=False,
            ), patch.object(maintenance, "datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 7, 11)
                mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
                removed = maintenance.prune_news_archive()

            saved = json.loads(archive_path.read_text(encoding="utf-8"))
            self.assertEqual(removed, 1)
            self.assertEqual([item["url"] for item in saved["articles"]], ["https://example.com/recent"])

if __name__ == "__main__":
    unittest.main()
