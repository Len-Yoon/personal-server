"""Typed provider boundaries with network doubles; no real message is sent."""
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/k8s"))
from ha_egress import providers


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.sender = providers.TelegramSender("12345:" + "x" * 24, "-12345")
        self.ack = {"ok": True, "result": {"message_id": 123, "chat": {"id": -12345},
                                         "private_field": "not-returned"}}

    def test_trusted_bot_chat_host_and_ack_projection(self):
        with patch.object(providers, "post_json", return_value=self.ack) as send:
            result = self.sender({"text": "fixture"}, timeout=1, deadline=time.monotonic() + 1)
        self.assertEqual(result.result, {"message_id": 123})
        args = send.call_args.args
        self.assertEqual(args[0], "api.telegram.org")
        self.assertEqual(args[2]["chat_id"], "-12345")
        self.assertNotIn("x" * 24, repr(self.sender))

    def test_body_cannot_select_credentials_recipient_or_operation(self):
        with patch.object(providers, "post_json") as send:
            for payload in ({"text": "fixture", "chat_id": "1"}, {"text": "fixture", "token": "x"},
                            {"url": "https://other"}, {"text": "x" * 4097}):
                with self.assertRaises(providers.ProviderUnavailable):
                    self.sender(payload, timeout=1, deadline=time.monotonic() + 1)
            send.assert_not_called()

    def test_missing_or_wrong_provider_ack_cannot_be_reported_as_completed(self):
        for response in ({"ok": True}, {"ok": False},
                         {"ok": True, "result": {"message_id": 123, "chat": {"id": 999}}}):
            with patch.object(providers, "post_json", return_value=response):
                with self.assertRaises(providers.ProviderUnavailable):
                    self.sender({"text": "fixture"}, timeout=1, deadline=time.monotonic() + 1)

    def test_expiry_after_dns_tls_prevents_first_network_send(self):
        connection = MagicMock()
        with patch.object(providers.http.client, "HTTPSConnection", return_value=connection), \
                patch.object(providers, "remaining", side_effect=[1, providers.ProviderUnavailable()]):
            with self.assertRaises(providers.ProviderUnavailable):
                providers.post_json("api.telegram.org", "/fixture", {}, timeout=1, deadline=time.monotonic() + 1)
        connection.connect.assert_called_once()
        connection.request.assert_not_called()
        connection.close.assert_called_once()

    def test_redirect_or_response_failure_is_not_retried_or_exposed(self):
        connection = MagicMock()
        connection.getresponse.return_value.status = 302
        with patch.object(providers.http.client, "HTTPSConnection", return_value=connection) as factory:
            with self.assertRaisesRegex(providers.ProviderUnavailable, "^effect_provider_unavailable$"):
                providers.post_json("api.telegram.org", "/fixture", {}, timeout=1, deadline=time.monotonic() + 1)
            self.assertEqual(factory.call_count, 1)
        connection.request.assert_called_once()

    def test_absolute_deadline_closes_saved_socket_during_continuous_slow_drip(self):
        import socket
        import ssl
        import threading
        wire,writer=socket.socketpair();stop=threading.Event();reads=[];sends=[]
        class Response:
            status=200
            length=None
            def isclosed(self):return False
            def read1(self,size):
                data=wire.recv(1);reads.append(data);return data
            def read(self,size):raise AssertionError('bulk_body_read_used')
        class Connection:
            def __init__(self):self.sock=wire;self.closed=False
            def connect(self):pass
            def request(self,*args):sends.append(args)
            def getresponse(self):
                # HTTP/1.0 can detach connection.sock before body consumption.
                self.sock=None
                return Response()
            def close(self):self.closed=True;wire.close()
        def drip():
            try:
                for _ in range(50):
                    if stop.is_set():break
                    writer.sendall(b' ')
                    if stop.wait(.015):break
            except OSError:pass
            finally:writer.close()
        thread=threading.Thread(target=drip,daemon=True);thread.start()
        connection=Connection();context=ssl.create_default_context();started=time.monotonic()
        try:
            with patch.object(providers.ssl,'create_default_context',return_value=context), \
                 patch.object(providers.http.client,'HTTPSConnection',return_value=connection) as factory:
                with self.assertRaisesRegex(providers.ProviderUnavailable,'^effect_provider_unavailable$'):
                    providers.post_json('api.telegram.org','/fixture',{},timeout=1,deadline=started+.12)
                self.assertEqual(factory.call_count,1)
            self.assertLess(time.monotonic()-started,.6)
            self.assertGreaterEqual(len(reads),2);self.assertEqual(len(sends),1)
            self.assertTrue(connection.closed)
        finally:
            stop.set();wire.close();thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
