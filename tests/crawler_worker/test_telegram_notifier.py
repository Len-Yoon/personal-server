import importlib
import json
import sys
import unittest
from unittest.mock import MagicMock, patch

from tests._test_support import prepare_service_import


INVESTING_WORLD = {"category": "KR_WORLD", "source": "Investing.com 한국어"}


class TelegramNotifierTests(unittest.TestCase):
    def reload_module(self):
        prepare_service_import("crawler-worker")
        import app.services.telegram_notifier as module

        return importlib.reload(module)

    def test_sends_alert_article_with_reason_and_publication_time(self):
        """Fails if the high-priority alert message loses its required metadata."""
        module = self.reload_module()
        response = MagicMock()
        response.__enter__.return_value = response

        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "TELEGRAM_CHAT_ID": "123456",
            },
            clear=False,
        ), patch.object(module, "urlopen", return_value=response) as mocked_urlopen:
            sent_count = module.notify_new_investing_articles(
                [
                    {
                        **INVESTING_WORLD,
                        "title_ko": "미 연준, 기준금리 동결 결정",
                        "published_at": "2026-08-10T09:00:00+09:00",
                        "nasdaq_relevance": {"level": "alert", "reasons": ["연준·금리"]},
                        "url": "https://kr.investing.com/news/stock-market-news/article-1",
                    }
                ]
            )

        self.assertEqual(sent_count, 1)
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.telegram.org/bottest-token/sendMessage")
        self.assertEqual(json.loads(request.data), {
            "chat_id": "123456",
            "text": (
                "[나스닥 중요 알림]\n미 연준, 기준금리 동결 결정\n"
                "이유: 연준·금리\n출처: Investing.com 한국어 · 2026-08-10T09:00:00+09:00\n"
                "https://kr.investing.com/news/stock-market-news/article-1"
            ),
            "disable_web_page_preview": True,
        })

    def test_does_not_send_when_telegram_configuration_is_missing(self):
        module = self.reload_module()

        with patch.dict(
            "os.environ",
            {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""},
            clear=False,
        ), patch.object(module, "urlopen") as mocked_urlopen:
            sent_count = module.notify_new_investing_articles(
                [{"title_ko": "미국 증시 상승", "url": "https://example.com/news"}]
            )

        self.assertEqual(sent_count, 0)
        mocked_urlopen.assert_not_called()

    def test_does_not_send_articles_without_alert_relevance(self):
        """Fails if archive-only articles bypass the classification gate."""
        module = self.reload_module()

        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "TELEGRAM_CHAT_ID": "123456",
            },
            clear=False,
        ), patch.object(module, "urlopen") as mocked_urlopen:
            sent_count = module.notify_new_investing_articles(
                [
                    {
                        **INVESTING_WORLD,
                        "title_ko": "엔비디아 목표주가 상향",
                        "nasdaq_relevance": {"level": "archive", "reasons": []},
                        "url": "https://kr.investing.com/news/nvidia",
                    }
                ]
            )

        self.assertEqual(sent_count, 0)
        mocked_urlopen.assert_not_called()

    def test_sends_general_market_articles_as_one_digest(self):
        """Fails if non-urgent market news must be sent one article at a time."""
        module = self.reload_module()
        response = MagicMock()
        response.__enter__.return_value = response

        with patch.dict(
            "os.environ",
            {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "123456"},
            clear=False,
        ), patch.object(module, "urlopen", return_value=response) as mocked_urlopen:
            sent = module.notify_market_news_digest(
                [
                    {
                        **INVESTING_WORLD,
                        "title": "미국 CPI 발표 결과",
                        "url": "https://kr.investing.com/news/cpi",
                        "market_topic": "미국",
                    },
                    {
                        **INVESTING_WORLD,
                        "title": "국제유가 WTI 상승",
                        "url": "https://kr.investing.com/news/oil",
                        "market_topic": "원유",
                    },
                ]
            )

        self.assertTrue(sent)
        payload = json.loads(mocked_urlopen.call_args.args[0].data)
        self.assertIn("[시장 뉴스 요약]", payload["text"])
        self.assertIn("미국 CPI 발표 결과", payload["text"])
        self.assertIn("국제유가 WTI 상승", payload["text"])

    def test_world_only_policy_applies_to_legacy_and_broker_delivery(self):
        module = self.reload_module()
        world = {
            **INVESTING_WORLD,
            "title": "연준 금리 결정",
            "url": "https://kr.investing.com/news/economy/article-1",
            "nasdaq_relevance": {"level": "alert", "reasons": ["금리"]},
        }
        excluded = [
            dict(world, category="KR_IT"),
            dict(world, category="KR_AI"),
            dict(world, source="Other RSS"),
            dict(world, url="https://kr.investing.com.example.org/news/article-1"),
        ]
        for broker in (False, True):
            with self.subTest(broker=broker), patch.dict(
                "os.environ", {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "123456"}
            ), patch.object(module, "_broker_enabled", return_value=broker), patch.object(
                module, "_broker_send", return_value=True
            ) as relay, patch.object(module, "urlopen") as direct:
                for article in excluded:
                    self.assertFalse(module.notify_market_news_digest([world, article]))
                self.assertEqual(module.notify_new_investing_articles(excluded), 0)
                relay.assert_not_called()
                direct.assert_not_called()
                self.assertTrue(module.notify_market_news_digest([world]))
                (relay if broker else direct).assert_called_once()


    def test_broker_send_precedes_token_check_and_never_falls_back(self):
        from types import SimpleNamespace
        module=self.reload_module()
        article={**INVESTING_WORLD,'title':'alert','url':'https://kr.investing.com/news/a','nasdaq_relevance':{'level':'alert','reasons':['rate']}}
        fake=SimpleNamespace(configuration=MagicMock(return_value={}),route=MagicMock(return_value={'message_id':7}))
        with patch.dict('os.environ',{'HA_PG_CONFIG':'signed-fixture','TELEGRAM_BOT_TOKEN':'','TELEGRAM_CHAT_ID':''}), patch.dict(sys.modules,{'ha_egress.client':fake}), patch.object(module,'urlopen') as raw:
            self.assertEqual(module.notify_new_investing_articles([article]),1)
            self.assertTrue(module.notify_market_news_digest([article]))
            self.assertTrue(all(call.args[0:2]==('crawler-worker','crawler.telegram.send') for call in fake.route.call_args_list))
            self.assertTrue(all(set(call.args[2])=={'text'} for call in fake.route.call_args_list))
            fake.route.side_effect=OSError('broker uncertain')
            self.assertEqual(module.notify_new_investing_articles([article]),0)
            self.assertFalse(module.notify_market_news_digest([article]))
            fake.route.side_effect=None;fake.route.return_value=None
            self.assertFalse(module.notify_market_news_digest([article]))
            raw.assert_not_called()

    def test_broker_configuration_failure_cannot_use_present_legacy_credentials(self):
        from types import SimpleNamespace
        module=self.reload_module();fake=SimpleNamespace(configuration=MagicMock(side_effect=OSError('bad binding')))
        with patch.dict('os.environ',{'HA_PG_CONFIG':'bad','TELEGRAM_BOT_TOKEN':'legacy','TELEGRAM_CHAT_ID':'123'}), patch.dict(sys.modules,{'ha_egress.client':fake}), patch.object(module,'urlopen') as raw:
            self.assertFalse(module.notify_market_news_digest([{**INVESTING_WORLD, 'title': 'x', 'url': 'https://kr.investing.com/news/x'}]))
            raw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
