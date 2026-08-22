from http.client import HTTPConnection
from threading import Event
import unittest

from app.services.oauth_callback import HyundaiOAuthCallbackServer


class _Authorizer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete_authorization(self, code: str, state: str) -> str:
        self.calls.append((code, state))
        return "현대 차량 연결이 완료되었습니다. Telegram으로 돌아가세요."


class HyundaiOAuthCallbackServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authorizer = _Authorizer()
        self.server = HyundaiOAuthCallbackServer(self.authorizer, host="127.0.0.1", port=0)
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()

    def _request(self, path: str) -> tuple[int, str]:
        connection = HTTPConnection("127.0.0.1", self.server.port, timeout=2)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
        return response.status, body

    def test_health_is_available_only_on_the_loopback_callback_server(self) -> None:
        status, body = self._request("/health")

        self.assertEqual(status, 200)
        self.assertEqual(body, "ok")

    def test_callback_forwards_code_and_state_without_rendering_them(self) -> None:
        status, body = self._request("/oauth/hyundai/callback?code=private-code&state=one-time-state")

        self.assertEqual(status, 200)
        self.assertIn("연결이 완료", body)
        self.assertNotIn("private-code", body)
        self.assertNotIn("one-time-state", body)
        self.assertEqual(self.authorizer.calls, [("private-code", "one-time-state")])

    def test_callback_rejects_missing_parameters(self) -> None:
        status, body = self._request("/oauth/hyundai/callback?code=private-code")

        self.assertEqual(status, 400)
        self.assertIn("잘못된", body)

    def test_unknown_path_is_not_exposed(self) -> None:
        status, _body = self._request("/anything-else")

        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
