import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from app.services.hyundai import HyundaiClient


class HyundaiClientTests(unittest.TestCase):
    def test_hyundai_client_returns_none_without_credentials(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("app.services.hyundai.urlopen") as mocked_urlopen:
            result = HyundaiClient.from_environment().fetch_snapshot()

        self.assertEqual(result.status, "disabled")
        mocked_urlopen.assert_not_called()

    def test_authorization_completion_rejects_wrong_or_reused_state(self) -> None:
        """Skipping state validation would let a forged public callback store another account's token."""
        with TemporaryDirectory() as temp_dir:
            client = HyundaiClient(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri="https://car.example.com/oauth/callback",
                token_store_path=Path(temp_dir) / "hyundai-token.json",
            )
            authorization = urlparse(client.begin_authorization())
            state = parse_qs(authorization.query)["state"][0]

            with self.assertRaises(ValueError):
                client.complete_authorization("authorization-code", "forged-state")
            state = parse_qs(urlparse(client.begin_authorization()).query)["state"][0]
            with patch("app.services.hyundai.urlopen", return_value=_Response(_token_payload())):
                client.complete_authorization("authorization-code", state)
            with self.assertRaises(ValueError):
                client.complete_authorization("authorization-code", state)

    def test_authorization_code_exchange_persists_refreshable_tokens(self) -> None:
        """Removing token persistence would force an OAuth login on every worker restart."""
        with TemporaryDirectory() as temp_dir:
            token_path = Path(temp_dir) / "hyundai-token.json"
            client = HyundaiClient(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri="https://car.example.com/oauth/callback",
                token_store_path=token_path,
                oauth_base_url="https://oauth.example.test/api/v1/user/oauth2",
                api_base_url="https://data.example.test/api/v1",
            )

            authorization = urlparse(client.authorization_url("csrf-state"))
            self.assertEqual(authorization.path, "/api/v1/user/oauth2/authorize")
            self.assertEqual(
                parse_qs(authorization.query),
                {
                    "response_type": ["code"],
                    "client_id": ["client-id"],
                    "redirect_uri": ["https://car.example.com/oauth/callback"],
                    "state": ["csrf-state"],
                },
            )

            with patch("app.services.hyundai.urlopen", return_value=_Response(_token_payload())):
                result = client.exchange_authorization_code("authorization-code")

            self.assertTrue(result)
            self.assertTrue(token_path.exists())
            self.assertEqual(client._load_tokens().refresh_token, "refresh-token")

    def test_fetch_snapshot_uses_registered_vehicle_endpoints(self) -> None:
        """Changing documented endpoint paths or response parsing must not silently disable alerts."""
        with TemporaryDirectory() as temp_dir:
            token_path = Path(temp_dir) / "hyundai-token.json"
            client = HyundaiClient(
                client_id="client-id",
                client_secret="client-secret",
                token_store_path=token_path,
                oauth_base_url="https://oauth.example.test/api/v1/user/oauth2",
                api_base_url="https://data.example.test/api/v1",
            )
            client._save_tokens_from_payload(_token_payload())
            responses = iter(
                [
                    _Response({"cars": [{"carId": "car-1", "carNickname": "Grandeur", "carType": "GN", "carName": "IG", "carSellname": "Grandeur"}], "msgId": "car-list"}),
                    _Response({"odometers": [{"date": "20260822", "timestamp": "20260822120000", "value": 52340, "unit": 1}], "msgId": "odometer"}),
                    _Response({"timestamp": "20260822120000", "value": 401.9, "unit": 1, "msgId": "dte"}),
                    _Response({"status": True, "msgId": "engine"}),
                    _Response({"status": False, "msgId": "brake"}),
                    _Response({"status": False, "msgId": "washer"}),
                    _Response({"status": False, "msgId": "fuel"}),
                ]
            )
            requested_urls: list[str] = []

            def open_request(request, **_kwargs):
                requested_urls.append(request.full_url)
                return next(responses)

            with patch("app.services.hyundai.urlopen", side_effect=open_request):
                result = client.fetch_snapshot()

        self.assertEqual(result.status, "success")
        self.assertEqual(result.snapshot.odometer_km, 52340)
        self.assertEqual(result.snapshot.dte_km, 401)
        self.assertEqual(result.snapshot.warnings, frozenset({"engine_oil"}))
        self.assertEqual(
            requested_urls,
            [
                "https://data.example.test/api/v1/car/profile/carlist",
                "https://data.example.test/api/v1/car/status/car-1/odometer",
                "https://data.example.test/api/v1/car/status/car-1/dte",
                "https://data.example.test/api/v1/car/status/warning/car-1/engineOil",
                "https://data.example.test/api/v1/car/status/warning/car-1/breakOil",
                "https://data.example.test/api/v1/car/status/warning/car-1/washerFluid",
                "https://data.example.test/api/v1/car/status/warning/car-1/lowFuel",
            ],
        )

    def test_completion_reports_failed_token_exchange_to_callback_server(self) -> None:
        """Returning a false success would make the public callback claim a vehicle is connected."""
        with TemporaryDirectory() as temp_dir:
            client = HyundaiClient(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri="https://car.example.com/oauth/callback",
                token_store_path=Path(temp_dir) / "hyundai-token.json",
            )
            state = parse_qs(urlparse(client.begin_authorization()).query)["state"][0]
            with patch("app.services.hyundai.urlopen", return_value=_Response({"errCode": "4002"})):
                with self.assertRaises(OSError):
                    client.complete_authorization("authorization-code", state)


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        import json

        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def _token_payload() -> dict:
    return {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "token_type": "Bearer",
        "expires_in": 7200,
    }


if __name__ == "__main__":
    unittest.main()
