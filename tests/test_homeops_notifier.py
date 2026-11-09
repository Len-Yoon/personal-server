import importlib
import json
import sys
import unittest
from unittest.mock import MagicMock, patch

from tests._test_support import prepare_service_import


class HomeOpsTelegramNotifierTests(unittest.TestCase):
    def reload_module(self):
        prepare_service_import("portal-web")
        import app.services.homeops_notifier as module

        return importlib.reload(module)

    def test_sends_redacted_restart_notification(self):
        module = self.reload_module()
        response = MagicMock()
        response.__enter__.return_value = response

        with patch.dict(
            "os.environ",
            {"HOMEOPS_TELEGRAM_BOT_TOKEN": "test-token", "HOMEOPS_TELEGRAM_CHAT_ID": "123456"},
            clear=False,
        ), patch.object(module, "urlopen", return_value=response) as mocked_urlopen:
            sent = module.HomeOpsTelegramNotifier().send(
                "container_restart_started",
                {"service": "crawler-worker", "reason": "healthcheck unhealthy", "admin_url": "https://admin.example/admin/status"},
            )

        self.assertTrue(sent)
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.telegram.org/bottest-token/sendMessage")
        self.assertEqual(json.loads(request.data), {
            "chat_id": "123456",
            "text": "[HomeOps] 컨테이너 재시작 시작\n서비스: crawler-worker\n사유: healthcheck unhealthy\n관리자: https://admin.example/admin/status",
            "disable_web_page_preview": True,
        })

    def test_does_not_send_without_homeops_configuration(self):
        module = self.reload_module()

        with patch.dict("os.environ", {"HOMEOPS_TELEGRAM_BOT_TOKEN": "", "HOMEOPS_TELEGRAM_CHAT_ID": ""}, clear=False), patch.object(module, "urlopen") as mocked_urlopen:
            sent = module.HomeOpsTelegramNotifier().send("host_memory_high", {"memory_percent": 91.0})

        self.assertFalse(sent)
        mocked_urlopen.assert_not_called()


    def test_signed_broker_sends_without_token_and_never_uses_raw_fallback(self):
        from types import SimpleNamespace
        module=self.reload_module();fake=SimpleNamespace(configuration=MagicMock(return_value={}),route=MagicMock(return_value={'message_id':8}))
        with patch.dict('os.environ',{'HA_PG_CONFIG':'signed','HOMEOPS_TELEGRAM_BOT_TOKEN':'','HOMEOPS_TELEGRAM_CHAT_ID':''}), patch.dict(sys.modules,{'ha_egress.client':fake}), patch.object(module,'urlopen') as raw:
            self.assertTrue(module.HomeOpsTelegramNotifier().send('container_restart_started',{'service':'book-memo'}))
            fake.route.assert_called_once_with('portal-web','portal.telegram.send',{'text':'[HomeOps] 컨테이너 재시작 시작\n서비스: book-memo'})
            fake.route.side_effect=OSError('uncertain')
            self.assertFalse(module.HomeOpsTelegramNotifier().send('container_restart_started',{}))
            fake.route.side_effect=None;fake.route.return_value=None
            self.assertFalse(module.HomeOpsTelegramNotifier().send('container_restart_started',{}))
            raw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
