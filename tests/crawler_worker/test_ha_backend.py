import copy
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import patch

from tests._test_support import prepare_service_import


class SharedState:
    DatabaseUnavailable = RuntimeError

    def __init__(self):
        self.rows = {}
        self.owner = True

    @contextmanager
    def state_transaction(self, service, key, default):
        value = copy.deepcopy(self.rows.get((service, key), default))
        yield value
        self.rows[service, key] = copy.deepcopy(value)

    def read_state(self, service, key, default):
        return copy.deepcopy(self.rows.get((service, key), default))

    def worker_owned(self, service):
        return self.owner

    @contextmanager
    def worker_scope(self, service):
        if not self.owner:
            raise RuntimeError("worker denied")
        yield


class CrawlerHATests(unittest.TestCase):
    def setUp(self):
        prepare_service_import("crawler-worker")
        from app.services import news_archive, news_collection_status, ha_backend
        self.archive = news_archive
        self.status = news_collection_status
        self.backend = SharedState()
        for module in (news_archive, news_collection_status, ha_backend):
            patched = patch.object(module, "runtime", return_value=self.backend)
            patched.start()
            self.addCleanup(patched.stop)

    def test_archive_read_modify_write_preserves_unknown_top_level_state(self):
        self.backend.rows["crawler-worker", "archive"] = {"articles": [], "custom_metadata": {"preserved": True}}
        with self.archive._archive_transaction():
            payload = self.archive._load_archive()
            payload["updated_at"] = "fixture"
            self.archive._save_archive(payload)
        self.assertEqual(self.backend.rows["crawler-worker", "archive"]["custom_metadata"], {"preserved": True})
        first = self.status.NewsCollectionStatusStore("/unused")
        second = self.status.NewsCollectionStatusStore("/also-unused")
        first.record_attempt()
        second.record_failure()
        self.assertEqual(first.snapshot()["failures_total"], 1)
        self.assertTrue(second.snapshot()["initialized"])

    def test_uncertain_provider_outcome_never_dispatches_again(self):
        event = {"event_id": "alert:fixture", "kind": "alert", "status": "pending",
                 "articles": [{"url": "https://example.test/fixture"}], "created_at": ""}
        self.backend.rows["crawler-worker", "archive"] = {"articles": [], "telegram_outbox": [event]}
        now = datetime.now(timezone.utc)
        with patch.object(self.archive, "notify_new_investing_articles", side_effect=OSError("ambiguous")) as send:
            with self.assertRaises(OSError):
                self.archive._send_pending_event(event["event_id"], now)
            self.assertTrue(self.archive._send_pending_event(event["event_id"], now))
        send.assert_called_once()
        self.assertEqual(self.backend.rows["crawler-worker", "archive"]["telegram_outbox"][0]["status"], "uncertain")

    def test_nonowner_get_collection_never_fetches_or_writes(self):
        self.backend.owner = False
        with patch.object(self.archive, "collect_korean_news_from_sources") as fetch:
            with self.assertRaises(RuntimeError):
                self.archive.collect_korean_news("KR_WORLD", force_refresh=True)
        fetch.assert_not_called()
        self.assertEqual(self.backend.rows, {})

    def test_nonowner_serves_shared_cached_articles_without_any_write_or_effect(self):
        self.backend.owner = False
        now = datetime.now(timezone.utc).isoformat()
        self.backend.rows["crawler-worker", "archive"] = {
            "articles": [{"url": "https://example.test/shared", "title": "fixture", "category": "KR_WORLD",
                          "collected_at": now, "published_at_sort": now, "expires_at": now}],
            "custom_metadata": {"preserved": True},
        }
        before = copy.deepcopy(self.backend.rows)
        with patch.object(self.archive, "collect_korean_news_from_sources") as fetch, patch.object(self.archive, "_schedule_refresh") as schedule, patch.object(self.archive, "_drain_notification_outbox") as dispatch:
            result = self.archive.collect_korean_news("KR_WORLD", force_refresh=True)
        self.assertEqual(result["articles"][0]["url"], "https://example.test/shared")
        self.assertTrue(result["cache"]["hit"])
        self.assertEqual(self.backend.rows, before)
        fetch.assert_not_called()
        schedule.assert_not_called()
        dispatch.assert_not_called()

    def test_failed_worker_observation_is_not_converted_to_cached_success(self):
        with patch.object(self.backend, "worker_owned", side_effect=RuntimeError("observation unavailable")), patch.object(self.backend, "read_state") as read:
            with self.assertRaises(RuntimeError):
                self.archive.collect_korean_news("KR_WORLD")
        read.assert_not_called()

    def test_stale_owner_refreshes_inline_without_competing_background_permit(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(seconds=self.archive.CACHE_TTL_SECONDS + 1)).isoformat()
        article = {"url": "https://example.test/stale", "title": "fixture", "category": "KR_WORLD",
                   "collected_at": old, "published_at_sort": now.isoformat(),
                   "expires_at": (now + timedelta(days=1)).isoformat()}
        self.backend.rows["crawler-worker", "archive"] = {"articles": [article]}
        with patch.object(self.archive, "_now", return_value=now), patch.object(self.archive, "collect_korean_news_from_sources", return_value=[]) as fetch, patch.object(self.archive, "_schedule_refresh") as schedule, patch.object(self.archive, "_drain_notification_outbox"):
            self.archive.collect_korean_news("KR_WORLD")
        fetch.assert_called_once()
        schedule.assert_not_called()
