import importlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tests._test_support import prepare_service_import


class CrawlerWorkerNewsServiceTests(unittest.TestCase):
    @staticmethod
    def timestamp(**delta):
        return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()

    def reload_news_archive(self):
        prepare_service_import("crawler-worker")
        import app.services.news_archive as news_archive

        return importlib.reload(news_archive)

    def tearDown(self):
        sys.modules.pop("feedparser", None)

    def test_news_archive_exposes_only_current_korean_collection_api(self):
        news_archive = self.reload_news_archive()

        self.assertFalse(hasattr(news_archive, "collect_market_news"))
        self.assertFalse(hasattr(news_archive, "get_categories"))

    def test_korean_news_hub_exposes_current_categories(self):
        news_archive = self.reload_news_archive()

        self.assertEqual(
            [item["code"] for item in news_archive.get_korean_categories()],
            ["KR_WORLD", "KR_IT", "KR_AI"],
        )

    def test_collect_korean_it_news_filters_non_korean_google_articles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")},
                clear=False,
            ):
                news_archive = self.reload_news_archive()
                with patch(
                    "app.services.news_sources.search_google_news_rss",
                    return_value=[
                        {
                            "url": "https://example.com/kr",
                            "title": "클라우드 도입이 빠르게 늘고 있다",
                            "summary": "국내 기업의 클라우드 전환이 늘고 있다.",
                            "source": "Google News",
                        },
                        {
                            "url": "https://example.com/en",
                            "title": "English headline",
                            "summary": "No Korean content",
                            "source": "Google News",
                        },
                    ],
                ):
                    result = news_archive.collect_korean_news("KR_IT", limit=5, force_refresh=True)

        self.assertEqual([item["url"] for item in result["articles"]], ["https://example.com/kr"])

    def test_collect_korean_world_news_uses_investing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")},
                clear=False,
            ):
                news_archive = self.reload_news_archive()
                with patch(
                    "app.services.news_sources.search_investing_news_rss",
                    return_value=[
                        {
                            "url": "https://example.com/world",
                            "title": "미 연준, 기준금리 동결 결정",
                            "title_ko": "미 연준, 기준금리 동결 결정",
                            "source": "Investing.com 한국어",
                        }
                    ],
                ) as mocked_investing:
                    result = news_archive.collect_korean_news("KR_WORLD", limit=1, force_refresh=True)

        self.assertEqual(result["articles"][0]["nasdaq_relevance"]["level"], "alert")
        mocked_investing.assert_called_once_with(limit=8)

    def test_world_news_result_exposes_alert_and_archive_counts(self):
        news_archive = self.reload_news_archive()

        result = news_archive._build_result(
            "KR_WORLD",
            [
                {
                    "title": "연준 금리 결정",
                    "nasdaq_relevance": {"level": "alert", "reasons": ["연준·금리"]},
                },
                {
                    "title": "일반 시장 기사",
                    "nasdaq_relevance": {"level": "archive", "reasons": []},
                },
                {"title": "분류 정보 없는 기사"},
            ],
            limit=24,
            cached=True,
            age_seconds=12,
            label_resolver=lambda category: category,
            description_resolver=lambda category: category,
        )

        self.assertEqual(
            result["relevance_summary"],
            {"total": 3, "alert": 1, "archive": 1, "unclassified": 1},
        )

    def test_background_korean_world_refresh_notifies_only_alert_articles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")},
                clear=False,
            ):
                news_archive = self.reload_news_archive()
                news_archive._save_archive(
                    {"updated_at": "", "articles": [], "telegram_notifications_initialized": True}
                )
                with patch.object(
                    news_archive,
                    "collect_korean_news_from_sources",
                    return_value=[
                        {
                            "url": "https://example.com/alert",
                            "title": "미 연준, 기준금리 동결 결정",
                            "title_ko": "미 연준, 기준금리 동결 결정",
                            "source": "Investing.com 한국어",
                        }
                    ],
                ), patch.object(news_archive, "notify_new_investing_articles") as notify:
                    news_archive._refresh_category("KR_WORLD", limit=1)

        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0][0]["url"], "https://example.com/alert")

    def test_load_archive_sanitizes_existing_html_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(archive_path)}, clear=False):
                news_archive = self.reload_news_archive()
                collected_at = self.timestamp(hours=-1)
                archive_path.write_text(
                    json.dumps(
                        {
                            "updated_at": "",
                            "articles": [
                                {
                                    "category": "KR_WORLD",
                                    "title": '<a href="https://example.com/a">시장 뉴스</a>',
                                    "title_ko": '<a href="https://example.com/a">시장 뉴스</a>',
                                    "title_original": '<a href="https://example.com/a">시장 뉴스</a>',
                                    "url": "https://example.com/a",
                                    "source": "<font>Investing.com 한국어</font>",
                                    "summary": '<a href="https://example.com/a">내용</a>',
                                    "collected_at": collected_at,
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                recent = news_archive.list_recent_news(korean_only=True)

        self.assertEqual(recent[0]["summary"], "내용")
        self.assertEqual(recent[0]["title_ko"], "시장 뉴스")


if __name__ == "__main__":
    unittest.main()
