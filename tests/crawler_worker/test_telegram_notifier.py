import importlib
import json
import sys
import unittest
from unittest.mock import MagicMock, patch

from tests._test_support import prepare_service_import


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
                        "title_ko": "미 연준, 기준금리 동결 결정",
                        "source": "Investing.com 한국어",
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
                        "title_ko": "엔비디아 목표주가 상향",
                        "nasdaq_relevance": {"level": "archive", "reasons": []},
                        "url": "https://example.com/nvidia",
                    }
                ]
            )

        self.assertEqual(sent_count, 0)
        mocked_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
