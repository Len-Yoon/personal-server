import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._test_support import prepare_service_import


class CrawlerWorkerNewsServiceTests(unittest.TestCase):
    def reload_news_archive(self):
        prepare_service_import("crawler-worker")
        import app.services.news_archive as news_archive

        return importlib.reload(news_archive)

    def tearDown(self):
        sys.modules.pop("feedparser", None)

    def test_collect_market_news_uses_cache_for_repeat_requests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict(
                "os.environ",
                {
                    "NEWS_ARCHIVE_PATH": str(archive_path),
                    "NEWS_REFRESH_INTERVAL_SECONDS": "3600",
                    "NEWS_RETENTION_DAYS": "7",
                },
                clear=False,
            ):
                news_archive = self.reload_news_archive()

                with patch(
                    "app.services.news_sources.search_investing_news_rss",
                    return_value=[
                        {
                            "url": "https://example.com/a",
                            "title": "세계 시장, 미국 금리 인하 기대에 안정세 유지",
                            "summary": "미국 금리 인하 기대와 달러 약세가 겹치며 글로벌 금융시장이 안정적인 흐름을 보였다.",
                            "source": "Reuters",
                        }
                    ],
                ) as mocked_investing, patch(
                    "app.services.news_sources.search_reuters_news_rss",
                    return_value=[],
                ) as mocked_reuters, patch(
                    "app.services.news_sources.search_ap_news_rss",
                    return_value=[],
                ) as mocked_ap, patch(
                    "app.services.news_sources.search_marketwatch_news_rss",
                    return_value=[],
                ) as mocked_marketwatch:
                    first = news_archive.collect_market_news("world", limit=1)
                    second = news_archive.collect_market_news("world", limit=1)

        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(mocked_investing.call_count, 1)
        self.assertEqual(mocked_reuters.call_count, 0)
        self.assertEqual(mocked_ap.call_count, 0)
        self.assertEqual(mocked_marketwatch.call_count, 0)

    def test_news_hub_exposes_market_topic_categories(self):
        news_archive = self.reload_news_archive()

        categories = news_archive.get_categories()

        self.assertEqual(
            [item["code"] for item in categories],
            ["INVESTING", "WORLD", "NASDAQ", "GOLD", "HK50"],
        )

    def test_korean_news_hub_exposes_korean_topic_categories(self):
        news_archive = self.reload_news_archive()

        categories = news_archive.get_korean_categories()

        self.assertEqual(
            [item["code"] for item in categories],
            ["KR_WORLD", "KR_IT", "KR_AI"],
        )
        self.assertEqual(categories[0]["label"], "Investing.com 뉴스")

    def test_collect_korean_news_uses_google_only_and_filters_english_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict(
                "os.environ",
                {
                    "NEWS_ARCHIVE_PATH": str(archive_path),
                    "NEWS_REFRESH_INTERVAL_SECONDS": "3600",
                    "NEWS_RETENTION_DAYS": "7",
                },
                clear=False,
            ):
                news_archive = self.reload_news_archive()

                with patch(
                    "app.services.news_sources.search_google_news_rss",
                    return_value=[
                        {
                            "url": "https://example.com/kr",
                            "title": "클라우드 도입이 빠르게 늘고 있다",
                            "summary": "국내 기업의 클라우드 전환이 늘면서 개발자 도구 수요도 함께 증가했다.",
                            "source": "Google News",
                        },
                        {
                            "url": "https://example.com/en",
                            "title": "English headline",
                            "summary": "No Korean content",
                            "source": "Google News",
                        },
                    ],
                ) as mocked_google, patch(
                    "app.services.news_sources.search_reuters_news_rss",
                    return_value=[],
                ) as mocked_reuters, patch(
                    "app.services.news_sources.search_ap_news_rss",
                    return_value=[],
                ) as mocked_ap, patch(
                    "app.services.news_sources.search_marketwatch_news_rss",
                    return_value=[],
                ) as mocked_marketwatch:
                    result = news_archive.collect_korean_news("kr_it", limit=1, force_refresh=True)

        self.assertEqual(result["category"], "KR_IT")
        self.assertEqual(len(result["articles"]), 1)
        self.assertEqual(result["articles"][0]["title"], "클라우드 도입이 빠르게 늘고 있다")
        mocked_google.assert_called_once()
        mocked_reuters.assert_not_called()
        mocked_ap.assert_not_called()
        mocked_marketwatch.assert_not_called()

    def test_collect_korean_world_news_uses_investing_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict(
                "os.environ",
                {
                    "NEWS_ARCHIVE_PATH": str(archive_path),
                    "NEWS_REFRESH_INTERVAL_SECONDS": "3600",
                    "NEWS_RETENTION_DAYS": "7",
                },
                clear=False,
            ):
                news_archive = self.reload_news_archive()

                with patch(
                    "app.services.news_sources.search_investing_news_rss",
                    return_value=[
                        {
                            "url": "https://example.com/world",
                            "title": "미국 금리와 세계 경제 동향",
                            "title_ko": "미국 금리와 세계 경제 동향",
                            "summary": "세계 경제 뉴스",
                            "source": "Investing.com 한국어",
                        }
                    ],
                ) as mocked_investing, patch(
                    "app.services.news_sources.search_google_news_rss",
                    return_value=[],
                ) as mocked_google:
                    result = news_archive.collect_korean_news(
                        "kr_world", limit=1, force_refresh=True
                    )

        self.assertEqual(result["category"], "KR_WORLD")
        self.assertEqual(len(result["articles"]), 1)
        mocked_investing.assert_called_once_with(limit=8)
        mocked_google.assert_not_called()

    def test_collect_korean_world_news_keeps_investing_items_without_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict(
                "os.environ",
                {"NEWS_ARCHIVE_PATH": str(archive_path)},
                clear=False,
            ):
                news_archive = self.reload_news_archive()

                with patch(
                    "app.services.news_sources.search_investing_news_rss",
                    return_value=[
                        {
                            "url": "https://example.com/investing-stock",
                            "title": "타슬리 제약 주가, 오늘 급등 이유는?",
                            "title_ko": "타슬리 제약 주가, 오늘 급등 이유는?",
                            "summary": "",
                            "source": "Investing.com 한국어",
                        }
                    ],
                ):
                    result = news_archive.collect_korean_news(
                        "kr_world", limit=1, force_refresh=True
                    )

        self.assertEqual(len(result["articles"]), 1)
        self.assertEqual(result["articles"][0]["title"], "타슬리 제약 주가, 오늘 급등 이유는?")

    def test_collect_market_news_uses_reuters_when_google_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict(
                "os.environ",
                {
                    "NEWS_ARCHIVE_PATH": str(archive_path),
                    "NEWS_REFRESH_INTERVAL_SECONDS": "3600",
                    "NEWS_RETENTION_DAYS": "7",
                },
                clear=False,
            ):
                news_archive = self.reload_news_archive()

                with patch(
                    "app.services.news_sources.search_google_news_rss",
                    return_value=[],
                ), patch(
                    "app.services.news_sources.search_reuters_news_rss",
                    return_value=[
                        {
                            "url": "https://example.com/r",
                            "title": "금 가격, 미국 금리 전망 변화에 소폭 상승",
                            "summary": "금 가격은 미국 금리 인하 기대와 안전자산 선호가 맞물리며 소폭 상승했다.",
                            "source": "Reuters",
                            "provider": "Reuters RSS",
                        }
                    ],
                ) as mocked_reuters, patch(
                    "app.services.news_sources.search_ap_news_rss",
                    return_value=[],
                ), patch(
                    "app.services.news_sources.search_marketwatch_news_rss",
                    return_value=[],
                ):
                    result = news_archive.collect_market_news("gold", limit=1, force_refresh=True)

        self.assertEqual(result["articles"][0]["provider"], "Reuters RSS")
        mocked_reuters.assert_called_once()

    def test_collect_market_news_returns_stale_cache_and_refreshes_in_background(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict(
                "os.environ",
                {
                    "NEWS_ARCHIVE_PATH": str(archive_path),
                    "NEWS_REFRESH_INTERVAL_SECONDS": "3600",
                    "NEWS_RETENTION_DAYS": "7",
                },
                clear=False,
            ):
                news_archive = self.reload_news_archive()
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                archive_path.write_text(
                    """
                    {
                      "updated_at": "2026-07-09T00:00:00+00:00",
                      "articles": [
                        {
                          "category": "WORLD",
                          "title": "A",
                          "title_ko": "A",
                          "title_original": "A",
                          "url": "https://example.com/a",
                          "source": "Reuters",
                          "published_at": "",
                          "summary": "",
                          "provider": "Reuters RSS",
                          "collected_at": "2026-07-08T00:00:00+00:00",
                          "expires_at": "2026-07-16T00:00:00+00:00"
                        }
                      ]
                    }
                    """.strip(),
                    encoding="utf-8",
                )

                with patch.object(news_archive, "_schedule_refresh") as mocked_refresh, patch(
                    "app.services.news_sources.search_google_news_rss",
                    return_value=[],
                ) as mocked_google, patch(
                    "app.services.news_sources.search_reuters_news_rss",
                    return_value=[],
                ) as mocked_reuters, patch(
                    "app.services.news_sources.search_ap_news_rss",
                    return_value=[],
                ) as mocked_ap, patch(
                    "app.services.news_sources.search_marketwatch_news_rss",
                    return_value=[],
                ) as mocked_marketwatch:
                    result = news_archive.collect_market_news("world", limit=1)

        self.assertTrue(result["cache"]["hit"])
        self.assertGreater(result["cache"]["age_seconds"], 3600)
        mocked_refresh.assert_called_once_with("WORLD", 1)
        mocked_google.assert_not_called()
        mocked_reuters.assert_not_called()
        mocked_ap.assert_not_called()
        mocked_marketwatch.assert_not_called()

    def test_collect_market_news_falls_back_to_empty_result_on_source_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict(
                "os.environ",
                {
                    "NEWS_ARCHIVE_PATH": str(archive_path),
                    "NEWS_REFRESH_INTERVAL_SECONDS": "3600",
                    "NEWS_RETENTION_DAYS": "7",
                },
                clear=False,
            ):
                news_archive = self.reload_news_archive()

                with patch(
                    "app.services.news_archive.collect_news_from_sources",
                    side_effect=RuntimeError("boom"),
                ):
                    result = news_archive.collect_market_news("gold", limit=1, force_refresh=True)

        self.assertEqual(result["count"], 0)
        self.assertEqual(result["articles"], [])

    def test_load_archive_sanitizes_existing_html_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict(
                "os.environ",
                {
                    "NEWS_ARCHIVE_PATH": str(archive_path),
                    "NEWS_REFRESH_INTERVAL_SECONDS": "3600",
                    "NEWS_RETENTION_DAYS": "7",
                },
                clear=False,
            ):
                news_archive = self.reload_news_archive()
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                archive_path.write_text(
                    """
                    {
                      "updated_at": "2026-07-09T00:00:00+00:00",
                      "articles": [
                        {
                          "category": "WORLD",
                          "title": "<a href=\\"https://example.com/a\\">시장 뉴스</a>",
                          "title_ko": "<a href=\\"https://example.com/a\\">시장 뉴스</a>",
                          "title_original": "<a href=\\"https://example.com/a\\">시장 뉴스</a>",
                          "url": "https://example.com/a",
                          "source": "<font>Reuters</font>",
                          "published_at": "",
                          "summary": "<a href=\\"https://example.com/a\\">내용</a>",
                          "provider": "<font>Reuters RSS</font>",
                          "collected_at": "2026-07-09T00:00:00+00:00",
                          "expires_at": "2026-07-16T00:00:00+00:00"
                        }
                      ]
                    }
                    """.strip(),
                    encoding="utf-8",
                )

                recent = news_archive.list_recent_news(limit=1)
                stored = json.loads(archive_path.read_text(encoding="utf-8"))

        self.assertEqual(recent[0]["summary"], "내용")
        self.assertEqual(recent[0]["title_ko"], "시장 뉴스")
        self.assertEqual(stored["articles"][0]["summary"], "내용")
        self.assertEqual(stored["articles"][0]["title"], "시장 뉴스")

    def test_list_recent_news_orders_by_collection_time(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict(
                "os.environ",
                {
                    "NEWS_ARCHIVE_PATH": str(archive_path),
                    "NEWS_REFRESH_INTERVAL_SECONDS": "3600",
                    "NEWS_RETENTION_DAYS": "7",
                },
                clear=False,
            ):
                news_archive = self.reload_news_archive()
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                archive_path.write_text(
                    """
                    {
                      "updated_at": "2026-07-09T00:00:00+00:00",
                          "articles": [
                        {
                          "category": "WORLD",
                          "title": "세계 시장, 미국 금리 인하 기대에 안정세 유지",
                          "title_ko": "세계 시장, 미국 금리 인하 기대에 안정세 유지",
                          "title_original": "세계 시장, 미국 금리 인하 기대에 안정세 유지",
                          "url": "https://example.com/a",
                          "source": "Reuters",
                          "published_at": "",
                          "summary": "",
                          "provider": "Reuters RSS",
                          "collected_at": "2026-07-09T00:00:00+00:00",
                          "expires_at": "2026-07-16T00:00:00+00:00"
                        },
                        {
                          "category": "GOLD",
                          "title": "금 가격, 미국 금리 전망 변화에 소폭 상승",
                          "title_ko": "금 가격, 미국 금리 전망 변화에 소폭 상승",
                          "title_original": "금 가격, 미국 금리 전망 변화에 소폭 상승",
                          "url": "https://example.com/b",
                          "source": "AP News",
                          "published_at": "",
                          "summary": "",
                          "provider": "AP News RSS",
                          "collected_at": "2026-07-09T01:00:00+00:00",
                          "expires_at": "2026-07-16T01:00:00+00:00"
                        }
                      ]
                    }
                    """.strip(),
                    encoding="utf-8",
                )

                recent = news_archive.list_recent_news(limit=10)

        self.assertEqual([item["url"] for item in recent], ["https://example.com/b", "https://example.com/a"])

    def test_list_recent_news_can_filter_to_korean_categories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict(
                "os.environ",
                {"NEWS_ARCHIVE_PATH": str(archive_path)},
                clear=False,
            ):
                news_archive = self.reload_news_archive()
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                archive_path.write_text(
                    """
                    {
                      "updated_at": "2026-07-09T00:00:00+00:00",
                      "articles": [
                        {"category": "KR_IT", "url": "https://example.com/kr", "collected_at": "2026-07-09T00:00:00+00:00"},
                        {"category": "NASDAQ", "url": "https://example.com/market", "collected_at": "2026-07-09T01:00:00+00:00"}
                      ]
                    }
                    """.strip(),
                    encoding="utf-8",
                )

                recent = news_archive.list_recent_news(korean_only=True)

        self.assertEqual([item["category"] for item in recent], ["KR_IT"])


if __name__ == "__main__":
    unittest.main()
