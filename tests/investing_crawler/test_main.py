import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from investing_crawler.app.main import run


class ImporterMainTests(unittest.TestCase):
    def test_run_creates_date_based_markdown_file(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "OBSIDIAN_VAULT_PATH": temp_dir,
                "OBSIDIAN_NEWS_DIR": "뉴스/Investing",
                "INVESTING_NEWS_URL": "https://kr.investing.com/news",
                "INVESTING_NEWS_LIMIT": "5",
                "INVESTING_NEWS_TIMEZONE": "Asia/Seoul",
            },
            clear=False,
        ), patch(
            "investing_crawler.app.main.collect_investing_news",
            new=Mock(return_value=[{
                "title": "테스트 뉴스",
                "url": "https://kr.investing.com/news/test",
                "published_label": "오늘",
                "published_at": "",
                "source": "Investing.com",
            }]),
        ) as collector:
            self.assertEqual(run(), 0)

            output_files = list(Path(temp_dir).rglob("*.md"))
            self.assertEqual(len(output_files), 1)
            self.assertIn("테스트 뉴스", output_files[0].read_text(encoding="utf-8"))
            collector.assert_called_once_with(
                limit=5,
                feed_url="https://kr.investing.com/news",
            )

    def test_run_preserves_existing_file_when_collection_is_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "OBSIDIAN_VAULT_PATH": temp_dir,
                "OBSIDIAN_NEWS_DIR": "뉴스/Investing",
                "INVESTING_NEWS_URL": "https://kr.investing.com/news",
                "INVESTING_NEWS_LIMIT": "5",
                "INVESTING_NEWS_TIMEZONE": "Asia/Seoul",
            },
            clear=False,
        ), patch(
            "investing_crawler.app.main.collect_investing_news",
            new=Mock(return_value=[]),
        ):
            output_dir = Path(temp_dir) / "뉴스" / "Investing"
            output_dir.mkdir(parents=True)
            output_file = output_dir / "existing.md"
            output_file.write_text("기존 내용\n", encoding="utf-8")

            self.assertEqual(run(), 1)
            self.assertEqual(output_file.read_text(encoding="utf-8"), "기존 내용\n")


if __name__ == "__main__":
    unittest.main()
