import unittest
from datetime import datetime

from investing_crawler.app.obsidian_writer import (
    merge_daily_markdown,
    render_daily_markdown,
)


class ObsidianWriterTests(unittest.TestCase):
    def test_render_daily_markdown_contains_metadata_only(self):
        output = render_daily_markdown(
            [{
                "title": "첫 번째 뉴스",
                "url": "https://kr.investing.com/news/a",
                "published_label": "5분 전",
                "published_at": "",
                "source": "Investing.com",
            }],
            datetime(2026, 7, 10, 6, 30),
            "https://kr.investing.com/news",
        )

        self.assertIn("# Investing.com 한국어 뉴스 - 2026-07-10", output)
        self.assertIn("[첫 번째 뉴스](https://kr.investing.com/news/a)", output)
        self.assertIn("게시 표시: 5분 전", output)
        self.assertNotIn("본문", output)

    def test_merge_daily_markdown_does_not_duplicate_existing_url(self):
        existing = (
            "- [기존 뉴스](https://kr.investing.com/news/a)\n"
            "  - 게시 표시: 어제\n"
            "  - 출처: Investing.com\n"
        )
        merged = merge_daily_markdown(
            existing,
            [{
                "title": "기존 뉴스",
                "url": "https://kr.investing.com/news/a",
                "published_label": "오늘",
                "published_at": "",
                "source": "Investing.com",
            }],
            datetime(2026, 7, 10, 6, 30),
            "https://kr.investing.com/news",
        )

        self.assertEqual(merged.count("https://kr.investing.com/news/a"), 1)


if __name__ == "__main__":
    unittest.main()
