import os
import unittest
from unittest.mock import patch

from app.services.hyundai import HyundaiClient


class HyundaiClientTests(unittest.TestCase):
    def test_hyundai_client_returns_none_without_credentials(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("app.services.hyundai.urlopen") as mocked_urlopen:
            result = HyundaiClient.from_environment().fetch_snapshot()

        self.assertEqual(result.status, "disabled")
        mocked_urlopen.assert_not_called()

    def test_hyundai_client_reports_request_and_invalid_response_as_errors(self) -> None:
        settings = {
            "HYUNDAI_CLIENT_ID": "id",
            "HYUNDAI_CLIENT_SECRET": "secret",
            "HYUNDAI_ACCESS_TOKEN": "token",
            "HYUNDAI_VEHICLE_ID": "vehicle",
            "HYUNDAI_API_URL": "https://example.invalid/status",
        }

        with patch.dict(os.environ, settings, clear=True), patch(
            "app.services.hyundai.urlopen", side_effect=OSError("unavailable")
        ):
            request_error = HyundaiClient.from_environment().fetch_snapshot()

        self.assertEqual(request_error.status, "error")
        self.assertEqual(request_error.error, "request")

        class Response:
            def read(self):
                return b"not-json"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        with patch.dict(os.environ, settings, clear=True), patch(
            "app.services.hyundai.urlopen", return_value=Response()
        ):
            parse_error = HyundaiClient.from_environment().fetch_snapshot()

        self.assertEqual(parse_error.status, "error")
        self.assertEqual(parse_error.error, "parse")


if __name__ == "__main__":
    unittest.main()
