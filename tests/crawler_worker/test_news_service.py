import importlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event, Thread, current_thread
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

    def test_archive_internal_modules_keep_storage_processing_and_notification_helpers(self):
        prepare_service_import("crawler-worker")
        from app.services import (
            news_archive_notifications,
            news_archive_processing,
            news_archive_storage,
        )

        self.assertEqual(
            news_archive_storage.empty_archive()["articles"],
            [],
        )
        self.assertEqual(
            news_archive_notifications.notification_articles([{"title": "기사"}, "invalid"]),
            [{"title": "기사"}],
        )
        self.assertTrue(
            news_archive_processing.same_market_event(
                {"title": "미국 CPI 발표 결과"},
                {"title": "미 CPI 결과 공개"},
            )
        )

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

    def test_collect_korean_news_displays_only_articles_published_today_in_korea(self):
        """Fresh collections must not reintroduce yesterday's article into the category page."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")},
                clear=False,
            ):
                news_archive = self.reload_news_archive()
                now = datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)
                with patch.object(news_archive, "_now", return_value=now), patch.object(
                    news_archive,
                    "collect_korean_news_from_sources",
                    return_value=[
                        {
                            "url": "https://example.com/today",
                            "title": "오늘의 IT 뉴스",
                            "title_ko": "오늘의 IT 뉴스",
                            "source": "Google News",
                            "published_at": "2026-08-20T00:30:00+00:00",
                        },
                        {
                            "url": "https://example.com/yesterday",
                            "title": "어제의 IT 뉴스",
                            "title_ko": "어제의 IT 뉴스",
                            "source": "Google News",
                            "published_at": "2026-08-19T14:00:00+00:00",
                        },
                    ],
                ):
                    result = news_archive.collect_korean_news("KR_IT", limit=5, force_refresh=True)

        self.assertEqual([article["url"] for article in result["articles"]], ["https://example.com/today"])

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

    def test_load_archive_invalidates_previous_broad_investing_cache(self):
        """Fails if articles collected under the broad RSS policy remain visible."""
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(archive_path)}, clear=False):
                news_archive = self.reload_news_archive()
                original = json.dumps(
                        {
                            "schema_version": "2026-07-15-korean-news-v2",
                            "updated_at": "",
                            "articles": [{"url": "https://example.com/unrelated"}],
                            "telegram_notifications_initialized": True,
                        },
                        ensure_ascii=False,
                    )
                archive_path.write_text(original, encoding="utf-8")

                archive = news_archive._load_archive()

                backup = archive_path.with_name("news_archive.json.v2.bak")
                self.assertTrue(backup.exists())
                self.assertEqual(backup.read_bytes(), original.encode("utf-8"))
                self.assertFalse(archive["telegram_notifications_initialized"])

        self.assertEqual(archive["schema_version"], news_archive.ARCHIVE_SCHEMA_VERSION)
        self.assertEqual(archive["articles"], [])

    def test_archive_rejects_corrupt_and_unknown_schema_without_rewrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(archive_path)}, clear=False):
                news_archive = self.reload_news_archive()
                for content in (
                    '{broken',
                    json.dumps({"schema_version": "future-v9", "articles": []}),
                    json.dumps({"schema_version": news_archive.ARCHIVE_SCHEMA_VERSION, "articles": [], "telegram_future_state": [1]}),
                ):
                    archive_path.write_text(content, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        news_archive._load_archive()
                    self.assertEqual(archive_path.read_text(encoding="utf-8"), content)

    def test_v2_backup_conflict_preserves_original_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            original = json.dumps({"schema_version": "2026-07-15-korean-news-v2", "articles": []})
            archive_path.write_text(original, encoding="utf-8")
            archive_path.with_name("news_archive.json.v2.bak").write_text("different", encoding="utf-8")
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(archive_path)}, clear=False):
                news_archive = self.reload_news_archive()
                with self.assertRaises(ValueError):
                    news_archive._load_archive()
            self.assertEqual(archive_path.read_text(encoding="utf-8"), original)

    def test_selects_one_article_per_topic_and_skips_similar_headlines(self):
        """Fails if a digest repeats the same topic or the same event."""
        news_archive = self.reload_news_archive()
        now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

        selected, remaining = news_archive._select_general_digest_articles(
            [
                {"title": "미국 CPI 발표 결과", "url": "https://example.com/cpi-1"},
                {"title": "미국 CPI 결과 발표", "url": "https://example.com/cpi-2"},
                {"title": "국제유가 WTI 상승", "url": "https://example.com/oil"},
                {"title": "나스닥 마감 상승", "url": "https://example.com/nasdaq"},
            ],
            now=now,
            topic_last_sent_at={},
            recent_sent_articles=[],
        )

        self.assertEqual(
            [article["url"] for article in selected],
            ["https://example.com/cpi-1", "https://example.com/oil", "https://example.com/nasdaq"],
        )
        self.assertEqual(remaining, [])

    def test_recognizes_differently_worded_headlines_for_the_same_event(self):
        """Fails if word-order changes bypass the non-AI duplicate check."""
        news_archive = self.reload_news_archive()

        self.assertTrue(
            news_archive._same_market_event(
                {"title": "미국 CPI 발표 결과"},
                {"title": "미 CPI 결과 공개"},
            )
        )
        self.assertFalse(
            news_archive._same_market_event(
                {"title": "미국 CPI 발표 결과"},
                {"title": "미국 고용 증가"},
            )
        )

    def test_keeps_follow_up_with_new_market_number_or_decision(self):
        """Fails if meaningful updates are discarded as duplicates."""
        news_archive = self.reload_news_archive()

        self.assertFalse(
            news_archive._same_market_event(
                {"title": "미국 CPI 발표 결과 3.0%"},
                {"title": "미국 CPI 발표 결과 3.2%"},
            )
        )
        self.assertFalse(
            news_archive._same_market_event(
                {"title": "연준 금리 동결 결정"},
                {"title": "연준 금리 발표"},
            )
        )

    def test_uses_fifteen_minute_digest_and_two_hour_deduplication_windows(self):
        """Fails if the alert policy becomes too slow or suppresses updates too long."""
        news_archive = self.reload_news_archive()

        self.assertEqual(news_archive.DIGEST_INTERVAL, timedelta(minutes=15))
        self.assertEqual(news_archive.DEDUPLICATION_WINDOW, timedelta(hours=2))

    def test_background_refresh_sends_non_urgent_market_news_as_digest_after_interval(self):
        """Fails if ordinary market news is never delivered after the digest interval."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")},
                clear=False,
            ):
                news_archive = self.reload_news_archive()
                now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
                news_archive._save_archive(
                    {
                        "updated_at": "",
                        "articles": [],
                        "telegram_notifications_initialized": True,
                        "telegram_last_digest_at": (now - timedelta(hours=1)).isoformat(),
                    }
                )
                with patch.object(news_archive, "_now", return_value=now), patch.object(
                    news_archive,
                    "collect_korean_news_from_sources",
                    return_value=[
                        {
                            "url": "https://example.com/oil",
                            "title": "국제유가 WTI 상승",
                            "title_ko": "국제유가 WTI 상승",
                            "source": "Investing.com 한국어",
                        }
                    ],
                ), patch.object(news_archive, "notify_market_news_digest", return_value=True) as notify:
                    news_archive._refresh_category("KR_WORLD", limit=1)

        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0][0]["market_topic"], "원유")

    def test_concurrent_direct_collections_preserve_both_articles(self):
        """Fails if two RSS snapshots overwrite each other's archive write."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")}, clear=False):
                news_archive = self.reload_news_archive()
                barrier = Barrier(2)
                errors = []

                def collect(category, limit):
                    barrier.wait(timeout=2)
                    return [{"url": f"https://example.com/{category}", "title": category, "source": "RSS"}]

                def run(category):
                    try:
                        news_archive.collect_korean_news(category, limit=1, force_refresh=True)
                    except Exception as error:  # pragma: no cover - asserted below
                        errors.append(error)

                with patch.object(news_archive, "collect_korean_news_from_sources", side_effect=collect):
                    threads = [Thread(target=run, args=(category,)) for category in ("KR_IT", "KR_AI")]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=3)

                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(errors, [])
                self.assertEqual(
                    {article["url"] for article in news_archive._load_archive()["articles"]},
                    {"https://example.com/KR_IT", "https://example.com/KR_AI"},
                )

    def test_committed_alert_is_persisted_as_pending_outbox_event_before_send(self):
        """Fails if an alert can be sent without a durable retry record."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")}, clear=False):
                news_archive = self.reload_news_archive()
                now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
                news_archive._save_archive({
                    "updated_at": "", "articles": [], "telegram_notifications_initialized": True,
                })
                article = news_archive._attach_archive_metadata(
                    {
                        "url": "https://example.com/alert",
                        "title": "미 연준 기준금리 동결",
                        "source": "Investing.com 한국어",
                    },
                    category="KR_WORLD",
                    now=now,
                )

                news_archive._commit_collected_articles("KR_WORLD", [article], now)

                outbox = news_archive._load_archive()["telegram_outbox"]
                self.assertEqual(len(outbox), 1)
                self.assertEqual(outbox[0]["kind"], "alert")
                self.assertEqual(outbox[0]["status"], "pending")
                self.assertEqual(outbox[0]["articles"], [article])

    def test_pending_alert_retries_after_failure_and_is_acknowledged_only_after_success(self):
        """Fails if a failed alert is discarded or a successful retry is not recorded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")}, clear=False):
                news_archive = self.reload_news_archive()
                now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
                article = {"url": "https://example.com/alert", "title": "중요 기사", "nasdaq_relevance": {"level": "alert", "reasons": ["금리"]}}
                event = news_archive.notification_event("alert", [article], now.isoformat())
                news_archive._save_archive({
                    "updated_at": "", "articles": [], "telegram_notifications_initialized": True,
                    "telegram_outbox": [event],
                })

                with patch.object(news_archive, "notify_new_investing_articles", return_value=0) as notify:
                    news_archive._drain_notification_outbox(now)
                self.assertEqual(notify.call_count, 1)
                self.assertEqual(news_archive._load_archive()["telegram_outbox"][0]["status"], "pending")

                with patch.object(news_archive, "notify_new_investing_articles", return_value=1) as notify:
                    news_archive._drain_notification_outbox(now)
                self.assertEqual(notify.call_count, 1)
                self.assertEqual(news_archive._load_archive()["telegram_outbox"][0]["status"], "sent")

    def test_digest_is_persisted_before_send_and_sent_event_is_not_replayed_after_reload(self):
        """Fails if digest selection has no durable event or sent events replay after restart."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")}, clear=False):
                news_archive = self.reload_news_archive()
                now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
                article = {"url": "https://example.com/digest", "title": "시장 뉴스", "market_topic": "미국"}
                news_archive._save_archive({
                    "updated_at": "", "articles": [], "telegram_notifications_initialized": True,
                    "telegram_last_digest_at": (now - timedelta(hours=1)).isoformat(),
                    "telegram_pending_articles": [article],
                })

                with patch.object(news_archive, "notify_market_news_digest", return_value=True) as notify:
                    news_archive._drain_notification_outbox(now)
                archive = news_archive._load_archive()
                self.assertEqual(notify.call_args.args[0], [article])
                self.assertEqual(archive["telegram_pending_articles"], [])
                self.assertEqual(archive["telegram_outbox"][0]["kind"], "digest")
                self.assertEqual(archive["telegram_outbox"][0]["status"], "sent")

                news_archive = self.reload_news_archive()
                with patch.object(news_archive, "notify_market_news_digest") as notify:
                    news_archive._drain_notification_outbox(now + timedelta(minutes=1))
                notify.assert_not_called()

    def test_drain_processes_starting_alerts_and_one_digest_without_consuming_new_events(self):
        """Fails if one drain stops after one alert or follows newly enqueued events forever."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")}, clear=False):
                news_archive = self.reload_news_archive()
                now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
                alerts = [
                    {"url": f"https://example.com/alert-{number}", "title": f"중요 {number}", "nasdaq_relevance": {"level": "alert", "reasons": ["금리"]}}
                    for number in (1, 2)
                ]
                digest_article = {"url": "https://example.com/digest", "title": "시장 뉴스", "market_topic": "미국"}
                news_archive._save_archive({
                    "updated_at": "", "articles": [], "telegram_notifications_initialized": True,
                    "telegram_last_digest_at": (now - timedelta(hours=1)).isoformat(),
                    "telegram_pending_articles": [digest_article],
                    "telegram_outbox": [news_archive.notification_event("alert", [article], now.isoformat()) for article in alerts],
                })

                with patch.object(news_archive, "notify_new_investing_articles", return_value=1) as alert_notify, patch.object(
                    news_archive, "notify_market_news_digest", return_value=True
                ) as digest_notify:
                    news_archive._drain_notification_outbox(now)

                self.assertEqual(alert_notify.call_count, 2)
                digest_notify.assert_called_once_with([digest_article])
                self.assertTrue(all(event["status"] == "sent" for event in news_archive._load_archive()["telegram_outbox"]))

    def test_outbox_failure_and_restart_contracts(self):
        """Fails if save failures send early or pending events lose their retry meaning after reload."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")}, clear=False):
                news_archive = self.reload_news_archive()
                now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
                alert = {"url": "https://example.com/alert-save", "title": "중요", "nasdaq_relevance": {"level": "alert", "reasons": ["금리"]}}
                event = news_archive.notification_event("alert", [alert], now.isoformat())
                news_archive._save_archive({"updated_at": "", "articles": [], "telegram_notifications_initialized": True, "telegram_outbox": [event]})

                with patch.object(news_archive, "notify_new_investing_articles", return_value=1) as notify, patch.object(
                    news_archive, "_save_archive", side_effect=OSError("disk full")
                ):
                    with self.assertRaises(OSError):
                        news_archive._drain_notification_outbox(now)
                notify.assert_not_called()
                self.assertEqual(news_archive._load_archive()["telegram_outbox"][0]["status"], "pending")

                news_archive = self.reload_news_archive()
                with patch.object(news_archive, "notify_new_investing_articles", return_value=1) as notify, patch.object(
                    news_archive, "_save_archive", side_effect=[None, OSError("ack disk full")]
                ):
                    with self.assertRaises(OSError):
                        news_archive._drain_notification_outbox(now)
                notify.assert_called_once()
                self.assertEqual(news_archive._load_archive()["telegram_outbox"][0]["status"], "pending")

                news_archive = self.reload_news_archive()
                with patch.object(news_archive, "notify_new_investing_articles", return_value=1) as notify:
                    news_archive._drain_notification_outbox(now)
                notify.assert_called_once()
                self.assertEqual(news_archive._load_archive()["telegram_outbox"][0]["status"], "sent")

    def test_digest_pending_event_retries_after_reload_without_cooldown_block(self):
        """Fails if a failed persisted digest is discarded or re-selection cooldown blocks its retry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")}, clear=False):
                news_archive = self.reload_news_archive()
                now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
                article = {"url": "https://example.com/digest-retry", "title": "시장", "market_topic": "미국"}
                event = news_archive.notification_event("digest", [article], now.isoformat())
                news_archive._save_archive({"updated_at": "", "articles": [], "telegram_notifications_initialized": True, "telegram_last_digest_at": now.isoformat(), "telegram_pending_articles": [article], "telegram_outbox": [event]})
                with patch.object(news_archive, "notify_market_news_digest", return_value=False) as notify:
                    news_archive._drain_notification_outbox(now)
                notify.assert_called_once_with([article])
                news_archive = self.reload_news_archive()
                with patch.object(news_archive, "notify_market_news_digest", return_value=True) as notify:
                    news_archive._drain_notification_outbox(now + timedelta(minutes=1))
                notify.assert_called_once_with([article])
                self.assertEqual(news_archive._load_archive()["telegram_outbox"][0]["status"], "sent")

    def test_outbox_normalization_pruning_and_digest_reservation_contract(self):
        """Fails if malformed state survives, IDs vary by order, old sent events persist, or reserved URLs reseat."""
        news_archive = self.reload_news_archive()
        now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
        first = {"url": "https://example.com/one", "title": "one"}
        second = {"url": "https://example.com/two", "title": "two"}
        self.assertEqual(
            news_archive.notification_event("digest", [first, second], now.isoformat())["event_id"],
            news_archive.notification_event("digest", [second, first], now.isoformat())["event_id"],
        )
        malformed = news_archive._notification_outbox([{"event_id": "bad", "kind": "alert", "status": "sent", "articles": [first], "sent_at": "not-a-time"}])
        self.assertEqual(news_archive._prune_sent_events(malformed, now), [])
        reserved = news_archive.notification_event("digest", [first], now.isoformat())
        archive = {"telegram_last_digest_at": "", "telegram_pending_articles": [first, second], "telegram_topic_last_sent_at": {}, "telegram_recent_articles": []}
        created = news_archive._create_pending_digest_event(archive, [reserved], now)
        self.assertIsNotNone(created)
        self.assertNotIn(first["url"], created["article_urls"])

    def test_commit_enqueue_save_failure_preserves_disk_and_never_reaches_notifier(self):
        """Fails if alert intent enqueue failure is hidden and later send code can run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")}, clear=False):
                news_archive = self.reload_news_archive()
                now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
                news_archive._save_archive({"updated_at": "", "articles": [], "telegram_notifications_initialized": True})
                article = news_archive._attach_archive_metadata({"url": "https://example.com/enqueue-fail", "title": "미 연준 기준금리 동결", "source": "Investing.com 한국어"}, "KR_WORLD", now)
                with patch.object(news_archive, "_save_archive", side_effect=OSError("enqueue disk full")), patch.object(news_archive, "notify_new_investing_articles") as notify:
                    with self.assertRaises(OSError):
                        news_archive._commit_collected_articles("KR_WORLD", [article], now)
                notify.assert_not_called()
                self.assertEqual(news_archive._load_archive()["telegram_outbox"], [])

    def test_sent_and_recent_retention_prune_at_two_hour_boundary_and_persist(self):
        """Fails if expired sent events or recent digest articles survive reload and suppress new news."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")}, clear=False):
                news_archive = self.reload_news_archive()
                now = datetime(2026, 8, 20, 12, 1, tzinfo=timezone.utc)
                article = {"url": "https://example.com/old", "title": "미국 CPI 발표", "market_topic": "미국"}
                event = dict(news_archive.notification_event("digest", [article], now.isoformat()), status="sent", sent_at=(now - timedelta(hours=2, seconds=1)).isoformat())
                news_archive._save_archive({"updated_at": "", "articles": [], "telegram_notifications_initialized": True, "telegram_outbox": [event], "telegram_recent_articles": [dict(article, sent_at=(now - timedelta(hours=2, seconds=1)).isoformat())]})
                news_archive._drain_notification_outbox(now)
                news_archive = self.reload_news_archive()
                archive = news_archive._load_archive()
                self.assertEqual(archive["telegram_outbox"], [])
                self.assertEqual(archive["telegram_recent_articles"], [])

    def test_digest_enqueue_and_ack_failures_preserve_retry_snapshot_on_disk(self):
        for failure_stage in ("enqueue", "ack"):
            with self.subTest(stage=failure_stage), tempfile.TemporaryDirectory() as tmpdir:
                with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "archive.json")}, clear=False):
                    news_archive = self.reload_news_archive()
                    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
                    article = {"url": "https://example.com/durable-digest", "title": "국제유가 상승", "market_topic": "원유"}
                    news_archive._save_archive({"articles": [], "telegram_notifications_initialized": True,
                                                "telegram_pending_articles": [article]})
                    save = news_archive._save_archive

                    def fail_at_event_transition(archive):
                        events = archive.get("telegram_outbox", [])
                        target = "pending" if failure_stage == "enqueue" else "sent"
                        if any(event["status"] == target for event in events):
                            raise OSError("injected persistence failure")
                        save(archive)

                    with patch.object(news_archive, "_save_archive", side_effect=fail_at_event_transition), patch.object(
                        news_archive, "notify_market_news_digest", return_value=True
                    ) as notify:
                        with self.assertRaises(OSError):
                            news_archive._drain_notification_outbox(now)
                    self.assertEqual(notify.call_count, 0 if failure_stage == "enqueue" else 1)
                    news_archive = self.reload_news_archive()
                    saved = news_archive._load_archive()
                    self.assertEqual(saved["telegram_pending_articles"], [article])
                    self.assertEqual([event["status"] for event in saved["telegram_outbox"]],
                                     [] if failure_stage == "enqueue" else ["pending"])
                    with patch.object(news_archive, "notify_market_news_digest", return_value=True) as retry:
                        news_archive._drain_notification_outbox(now + timedelta(minutes=1))
                    retry.assert_called_once_with([article])
                    self.assertEqual(news_archive._load_archive()["telegram_pending_articles"], [])
                    self.assertEqual(news_archive._load_archive()["telegram_outbox"][0]["status"], "sent")

    def test_legacy_and_malformed_outbox_preserve_existing_article_and_pending_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "archive.json")}, clear=False):
                news_archive = self.reload_news_archive()
                now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
                article = news_archive._attach_archive_metadata(
                    {"url": "https://example.com/legacy", "title": "시장 동향", "source": "RSS"}, "KR_IT", now)
                news_archive._save_archive({"articles": [article], "telegram_notifications_initialized": True,
                                            "telegram_pending_articles": [article]})
                legacy = news_archive._load_archive()
                self.assertEqual(legacy["telegram_outbox"], [])
                legacy["telegram_outbox"] = [None, {}, {"event_id": "bad", "kind": "unknown"}]
                news_archive._save_archive(legacy)
                restored = news_archive._load_archive()
                self.assertEqual(restored["articles"], legacy["articles"])
                self.assertEqual(restored["telegram_pending_articles"], [article])
                self.assertEqual(restored["telegram_outbox"], [])
                old = news_archive.notification_event("alert", [article], now.isoformat())
                old.update(status="sent", sent_at="2026-08-20T07:00:00")
                self.assertEqual(news_archive._prune_sent_events([old], now), [])

    def test_digest_ack_preserves_pending_added_while_notifier_runs(self):
        """Fails if a successful digest ack writes a stale pending snapshot."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")}, clear=False):
                news_archive = self.reload_news_archive()
                now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
                news_archive._save_archive({
                    "updated_at": "", "articles": [], "telegram_notifications_initialized": True,
                    "telegram_last_digest_at": (now - timedelta(hours=1)).isoformat(),
                    "telegram_pending_articles": [{"url": "https://example.com/selected", "title": "국제유가 상승"}],
                })
                sending = Event()
                release = Event()
                sender_errors = []

                def notify(_articles):
                    sending.set()
                    self.assertTrue(release.wait(timeout=2))
                    return True

                with patch.object(news_archive, "notify_market_news_digest", side_effect=notify):
                    def send_digest_from_current_snapshot():
                        try:
                            news_archive._queue_and_send_general_digest(now)
                        except Exception as error:  # pragma: no cover - asserted below
                            sender_errors.append(error)

                    sender = Thread(target=send_digest_from_current_snapshot)
                    sender.start()
                    try:
                        self.assertTrue(sending.wait(timeout=2))
                        news_archive._commit_collected_articles(
                            "KR_WORLD",
                            [news_archive._attach_archive_metadata(
                                {"url": "https://example.com/new", "title": "미 연준 기준금리 동결", "source": "Investing.com 한국어"},
                                category="KR_WORLD",
                                now=now,
                            )],
                            now,
                        )
                        with news_archive._ARCHIVE_WRITE_LOCK:
                            latest = news_archive._load_archive()
                            latest["telegram_pending_articles"].append(
                                {"url": "https://example.com/concurrent-general", "title": "기업 실적"})
                            news_archive._save_archive(latest)
                    finally:
                        release.set()
                        sender.join(timeout=3)

                self.assertFalse(sender.is_alive())
                self.assertEqual(sender_errors, [])
                latest = news_archive._load_archive()
                self.assertEqual([article["url"] for article in latest["telegram_pending_articles"]],
                                 ["https://example.com/concurrent-general"])
                self.assertIn("https://example.com/new", {article["url"] for article in latest["articles"]})
                outbox = latest["telegram_outbox"]
                self.assertTrue(any(event["kind"] == "alert" and event["status"] == "pending" and event["article_urls"] == ["https://example.com/new"] for event in outbox))

    def test_direct_and_background_collections_preserve_both_articles(self):
        """Fails if a background refresh saves an archive snapshot from before a direct refresh."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")}, clear=False):
                news_archive = self.reload_news_archive()
                barrier = Barrier(2)
                errors = []

                def collect(category, limit):
                    barrier.wait(timeout=2)
                    return [{"url": f"https://example.com/{category}", "title": category, "source": "RSS"}]

                def direct():
                    try:
                        news_archive.collect_korean_news("KR_IT", limit=1, force_refresh=True)
                    except Exception as error:  # pragma: no cover - asserted below
                        errors.append(error)

                with patch.object(news_archive, "collect_korean_news_from_sources", side_effect=collect):
                    thread = Thread(target=direct)
                    thread.start()
                    news_archive._refresh_category("KR_AI", limit=1)
                    thread.join(timeout=3)

                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(
                    {article["url"] for article in news_archive._load_archive()["articles"]},
                    {"https://example.com/KR_IT", "https://example.com/KR_AI"},
                )

    def test_rss_and_telegram_calls_do_not_hold_archive_lock(self):
        """Fails if an external RSS or Telegram call blocks archive writers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")}, clear=False):
                news_archive = self.reload_news_archive()
                now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
                news_archive._save_archive({
                    "updated_at": "", "articles": [], "telegram_notifications_initialized": True,
                    "telegram_last_digest_at": (now - timedelta(hours=1)).isoformat(),
                    "telegram_pending_articles": [{"url": "https://example.com/oil", "title": "국제유가 상승"}],
                })

                def lock_is_available():
                    acquired = []
                    def probe():
                        got_lock = news_archive._ARCHIVE_WRITE_LOCK.acquire(blocking=False)
                        acquired.append(got_lock)
                        if got_lock:
                            news_archive._ARCHIVE_WRITE_LOCK.release()
                    thread = Thread(target=probe)
                    thread.start()
                    thread.join(timeout=2)
                    self.assertFalse(thread.is_alive())
                    return acquired == [True]

                with patch.object(
                    news_archive,
                    "collect_korean_news_from_sources",
                    side_effect=lambda **_kwargs: (self.assertTrue(lock_is_available()) or []),
                ), patch.object(
                    news_archive,
                    "notify_market_news_digest",
                    side_effect=lambda _articles: self.assertTrue(lock_is_available()) or True,
                ):
                    news_archive.collect_korean_news("KR_IT", limit=1, force_refresh=True)
                    news_archive._queue_and_send_general_digest(now)

    def test_digest_failure_or_exception_keeps_pending_articles(self):
        """Fails if a failed digest acknowledges selected pending articles."""
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(archive_path)}, clear=False):
                news_archive = self.reload_news_archive()
                now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

                for notifier in (False, RuntimeError("telegram unavailable")):
                    news_archive._save_archive({
                        "updated_at": "", "articles": [], "telegram_notifications_initialized": True,
                        "telegram_last_digest_at": (now - timedelta(hours=1)).isoformat(),
                        "telegram_pending_articles": [{"url": "https://example.com/oil", "title": "국제유가 상승"}],
                    })
                    patch_args = {"side_effect": notifier} if isinstance(notifier, Exception) else {"return_value": notifier}
                    with patch.object(news_archive, "notify_market_news_digest", **patch_args):
                        if isinstance(notifier, Exception):
                            with self.assertRaises(RuntimeError):
                                news_archive._queue_and_send_general_digest(now)
                        else:
                            news_archive._queue_and_send_general_digest(now)

                    self.assertEqual(
                        [article["url"] for article in news_archive._load_archive()["telegram_pending_articles"]],
                        ["https://example.com/oil"],
                    )

    def test_concurrent_general_articles_enqueue_once_each(self):
        """Fails if concurrent ordinary KR_WORLD collection loses or duplicates pending URLs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")}, clear=False):
                news_archive = self.reload_news_archive()
                now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
                news_archive._save_archive({
                    "updated_at": "", "articles": [], "telegram_notifications_initialized": True,
                    "telegram_last_digest_at": now.isoformat(),
                    "telegram_pending_articles": [{"url": "https://example.com/existing", "title": "기존 시장 기사"}],
                })
                barrier = Barrier(2)
                errors = []

                def collect(category, limit):
                    barrier.wait(timeout=2)
                    article_id = current_thread().name
                    return [{"url": f"https://example.com/{article_id}", "title": f"{article_id} 시장 기사", "source": "Investing.com 한국어"}]

                def run(category):
                    try:
                        news_archive.collect_korean_news(category, limit=1, force_refresh=True)
                    except Exception as error:  # pragma: no cover - asserted below
                        errors.append(error)

                with patch.object(news_archive, "_now", return_value=now), patch.object(
                    news_archive, "collect_korean_news_from_sources", side_effect=collect
                ), patch.object(news_archive, "notify_market_news_digest") as digest_notify, patch.object(
                    news_archive, "notify_new_investing_articles"
                ):
                    threads = [Thread(target=run, args=("KR_WORLD",), name=f"pending-{index}") for index in (1, 2)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=3)

                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(errors, [])
                pending_urls = [
                    article["url"] for article in news_archive._load_archive()["telegram_pending_articles"]
                ]
                self.assertEqual(
                    set(pending_urls),
                    {"https://example.com/existing", "https://example.com/pending-1", "https://example.com/pending-2"},
                )
                self.assertEqual(len(pending_urls), len(set(pending_urls)))
                digest_notify.assert_not_called()

    def test_list_purge_and_normalize_do_not_overwrite_concurrent_collection(self):
        """Fails if list purge saves its stale normalized snapshot after collection commits."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "news_archive.json")}, clear=False):
                news_archive = self.reload_news_archive()
                now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
                retained = {
                    "url": "https://example.com/retained", "title": "보존 기사", "summary": "<b>정규화 내용</b>",
                    "category": "KR_IT", "collected_at": now.isoformat(), "expires_at": (now + timedelta(days=1)).isoformat(),
                }
                expired = {
                    "url": "https://example.com/expired", "title": "만료 기사", "category": "KR_IT",
                    "collected_at": (now - timedelta(days=10)).isoformat(), "expires_at": (now - timedelta(days=1)).isoformat(),
                }
                news_archive._save_archive({"updated_at": "", "articles": [retained, expired], "telegram_notifications_initialized": True})
                entered_purge = Event()
                release_purge = Event()
                errors = []
                original_purge = news_archive._purge_archive

                def purge_with_list_pause(archive, current_now):
                    if current_thread().name == "list-purge-thread":
                        entered_purge.set()
                        self.assertTrue(release_purge.wait(timeout=2))
                    return original_purge(archive, current_now)

                def list_articles():
                    try:
                        news_archive.list_recent_news(korean_only=True)
                    except Exception as error:  # pragma: no cover - asserted below
                        errors.append(error)

                def collect_article():
                    try:
                        news_archive.collect_korean_news("KR_AI", limit=1, force_refresh=True)
                    except Exception as error:  # pragma: no cover - asserted below
                        errors.append(error)

                with patch.object(news_archive, "_now", return_value=now), patch.object(
                    news_archive, "_purge_archive", side_effect=purge_with_list_pause
                ), patch.object(
                    news_archive, "collect_korean_news_from_sources",
                    return_value=[{"url": "https://example.com/new", "title": "새 기사", "source": "RSS"}],
                ):
                    lister = Thread(target=list_articles, name="list-purge-thread")
                    lister.start()
                    collector = None
                    try:
                        self.assertTrue(entered_purge.wait(timeout=2))
                        collector = Thread(target=collect_article, name="collector-thread")
                        collector.start()
                    finally:
                        release_purge.set()
                        lister.join(timeout=3)
                        if collector is not None:
                            collector.join(timeout=3)

                self.assertFalse(lister.is_alive())
                self.assertIsNotNone(collector)
                self.assertFalse(collector.is_alive())
                self.assertEqual(errors, [])
                archive = news_archive._load_archive()
                self.assertEqual(
                    {article["url"] for article in archive["articles"]},
                    {"https://example.com/retained", "https://example.com/new"},
                )
                self.assertEqual(
                    next(article for article in archive["articles"] if article["url"] == "https://example.com/retained")["summary"],
                    "정규화 내용",
                )


if __name__ == "__main__":
    unittest.main()
