from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.models import VehicleSnapshot
from app.services.vehicle_monitor import SUPPORTED_WARNINGS


DEFAULT_OAUTH_BASE_URL = "https://prd.kr-ccapi.hyundai.com/api/v1/user/oauth2"
DEFAULT_API_BASE_URL = "https://dev.kr-ccapi.hyundai.com/api/v1"
DEFAULT_TOKEN_STORE_PATH = "/data/car-care/hyundai-token.json"


@dataclass(frozen=True)
class HyundaiFetchResult:
    status: str
    snapshot: VehicleSnapshot | None = None
    error: str | None = None

    @classmethod
    def disabled(cls) -> "HyundaiFetchResult":
        return cls("disabled")

    @classmethod
    def success(cls, snapshot: VehicleSnapshot) -> "HyundaiFetchResult":
        return cls("success", snapshot=snapshot)

    @classmethod
    def failure(cls, error: str) -> "HyundaiFetchResult":
        return cls("error", error=error)


@dataclass(frozen=True)
class _Tokens:
    access_token: str
    refresh_token: str
    expires_at: float


class HyundaiClient:
    """Hyundai Developers OAuth 2.0 and vehicle-data client.

    OAuth currently uses Hyundai's production host while development vehicle
    data uses its development host. Both bases are configurable to make a
    commercial-project move explicit instead of silently changing endpoints.
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        vehicle_id: str | None = None,
        *,
        redirect_uri: str | None = None,
        token_store_path: Path | str | None = None,
        oauth_base_url: str = DEFAULT_OAUTH_BASE_URL,
        api_base_url: str = DEFAULT_API_BASE_URL,
    ) -> None:
        self._client_id = _clean(client_id)
        self._client_secret = _clean(client_secret)
        self._vehicle_id = _clean(vehicle_id)
        self._redirect_uri = _clean(redirect_uri)
        self._token_store_path = Path(token_store_path or DEFAULT_TOKEN_STORE_PATH)
        self._oauth_base_url = oauth_base_url.rstrip("/")
        self._api_base_url = api_base_url.rstrip("/")

    @classmethod
    def from_environment(cls) -> "HyundaiClient":
        return cls(
            client_id=os.getenv("HYUNDAI_CLIENT_ID"),
            client_secret=os.getenv("HYUNDAI_CLIENT_SECRET"),
            vehicle_id=os.getenv("HYUNDAI_VEHICLE_ID"),
            redirect_uri=os.getenv("HYUNDAI_REDIRECT_URI"),
            token_store_path=os.getenv("HYUNDAI_TOKEN_STORE_PATH") or DEFAULT_TOKEN_STORE_PATH,
            oauth_base_url=os.getenv("HYUNDAI_OAUTH_BASE_URL") or DEFAULT_OAUTH_BASE_URL,
            api_base_url=os.getenv("HYUNDAI_API_BASE_URL") or DEFAULT_API_BASE_URL,
        )

    def authorization_url(self, state: str) -> str:
        if not all((self._client_id, self._redirect_uri, state)):
            raise ValueError("HYUNDAI_CLIENT_ID, HYUNDAI_REDIRECT_URI, and state are required")
        return f"{self._oauth_base_url}/authorize?" + urlencode(
            {"response_type": "code", "client_id": self._client_id, "redirect_uri": self._redirect_uri, "state": state}
        )

    def begin_authorization(self) -> str:
        """Create and persist one short-lived CSRF state for the public callback."""
        state = secrets.token_urlsafe(32)
        self._save_authorization_state(state)
        return self.authorization_url(state)

    def complete_authorization(self, code: str, state: str | None = None) -> str:
        """Exchange an account-redirect authorization code and persist its token pair."""
        self._consume_authorization_state(state)
        if not self.exchange_authorization_code(code):
            raise OSError("Hyundai token exchange failed")
        return "현대 차량 연결이 완료되었습니다. Telegram으로 돌아가세요."

    def exchange_authorization_code(self, code: str) -> bool:
        if not all((self._client_id, self._client_secret, self._redirect_uri, _clean(code))):
            return False
        return self._save_tokens_from_payload(self._token_request({
            "grant_type": "authorization_code", "code": code, "redirect_uri": self._redirect_uri,
        }))

    def fetch_snapshot(self) -> HyundaiFetchResult:
        if not all((self._client_id, self._client_secret)):
            return HyundaiFetchResult.disabled()
        token = self._valid_access_token()
        if token is None:
            return HyundaiFetchResult.disabled()
        try:
            car_id = self._vehicle_id or self._resolve_single_car(token)
            if car_id is None:
                return HyundaiFetchResult.failure("vehicle")
            odometer_payload = self._get_json(f"/car/status/{car_id}/odometer", token)
            dte_payload = self._get_json(f"/car/status/{car_id}/dte", token)
            odometer = _odometer_km(odometer_payload)
            if odometer is None or dte_payload is None:
                return HyundaiFetchResult.failure("response")
            warnings = self._fetch_warnings(car_id, token)
            if warnings is None:
                return HyundaiFetchResult.failure("response")
        except (OSError, URLError, HTTPError):
            return HyundaiFetchResult.failure("request")
        except (ValueError, json.JSONDecodeError):
            return HyundaiFetchResult.failure("parse")
        return HyundaiFetchResult.success(
            VehicleSnapshot(datetime.now(timezone.utc), odometer, _distance_km(dte_payload), frozenset(warnings))
        )

    def _valid_access_token(self) -> str | None:
        tokens = self._load_tokens()
        if tokens is None:
            return None
        if tokens.expires_at > time.time() + 60:
            return tokens.access_token
        if not self._save_tokens_from_payload(self._token_request({"grant_type": "refresh_token", "refresh_token": tokens.refresh_token})):
            return None
        refreshed = self._load_tokens()
        return None if refreshed is None else refreshed.access_token

    def _resolve_single_car(self, access_token: str) -> str | None:
        payload = self._get_json("/car/profile/carlist", access_token)
        if not isinstance(payload, dict) or not isinstance(payload.get("cars"), list) or len(payload["cars"]) != 1:
            return None
        car = payload["cars"][0]
        return _clean(car.get("carId")) if isinstance(car, dict) else None

    def _fetch_warnings(self, car_id: str, access_token: str) -> set[str] | None:
        paths = {"engine_oil": "engineOil", "brake_oil": "breakOil", "washer_fluid": "washerFluid", "fuel": "lowFuel"}
        active: set[str] = set()
        for warning, path in paths.items():
            payload = self._get_json(f"/car/status/warning/{car_id}/{path}", access_token)
            if not isinstance(payload, dict) or not isinstance(payload.get("status"), bool):
                return None
            if payload["status"] and warning in SUPPORTED_WARNINGS:
                active.add(warning)
        return active

    def _get_json(self, path: str, access_token: str) -> object:
        request = Request(f"{self._api_base_url}{path}", headers={
            "Authorization": f"Bearer {access_token}", "Content-Type": "application/json",
        }, method="GET")
        with urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))

    def _token_request(self, fields: dict[str, str]) -> object | None:
        if not all((self._client_id, self._client_secret)):
            return None
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        request = Request(f"{self._oauth_base_url}/token", data=urlencode(fields).encode(), headers={
            "Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded",
        }, method="POST")
        try:
            with urlopen(request, timeout=8) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, HTTPError, ValueError, json.JSONDecodeError):
            return None

    def _save_tokens_from_payload(self, payload: object | None) -> bool:
        if not isinstance(payload, dict):
            return False
        access_token, refresh_token, expires_in = _clean(payload.get("access_token")), _clean(payload.get("refresh_token")), payload.get("expires_in")
        if not access_token or not refresh_token or not isinstance(expires_in, (int, float, str)):
            return False
        try:
            expires_at = time.time() + float(expires_in)
        except ValueError:
            return False
        self._save_tokens(_Tokens(access_token, refresh_token, expires_at))
        return True

    def _save_tokens(self, tokens: _Tokens) -> None:
        payload = self._load_token_file()
        payload.update({"access_token": tokens.access_token, "refresh_token": tokens.refresh_token, "expires_at": tokens.expires_at})
        self._write_token_file(payload)

    def _load_tokens(self) -> _Tokens | None:
        payload = self._load_token_file()
        access_token, refresh_token, expires_at = _clean(payload.get("access_token")), _clean(payload.get("refresh_token")), payload.get("expires_at")
        if not access_token or not refresh_token or not isinstance(expires_at, (int, float)):
            return None
        return _Tokens(access_token, refresh_token, float(expires_at))

    def _save_authorization_state(self, state: str) -> None:
        payload = self._load_token_file()
        payload.update({"authorization_state": state, "authorization_state_expires_at": time.time() + 600})
        self._write_token_file(payload)

    def _consume_authorization_state(self, state: str | None) -> None:
        payload = self._load_token_file()
        expected, expires_at = _clean(payload.get("authorization_state")), payload.get("authorization_state_expires_at")
        payload.pop("authorization_state", None)
        payload.pop("authorization_state_expires_at", None)
        self._write_token_file(payload)
        if not state or not expected or not isinstance(expires_at, (int, float)) or expires_at <= time.time() or not secrets.compare_digest(state, expected):
            raise ValueError("invalid authorization state")

    def _load_token_file(self) -> dict:
        try:
            payload = json.loads(self._token_store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_token_file(self, payload: dict) -> None:
        self._token_store_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._token_store_path.with_suffix(self._token_store_path.suffix + ".tmp")
        temporary_path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(self._token_store_path)


def _clean(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _odometer_km(payload: object) -> int | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("odometers"), list) or not payload["odometers"]:
        return None
    return _distance_km(payload["odometers"][0])


def _distance_km(payload: object) -> int | None:
    if not isinstance(payload, dict):
        return None
    value, unit = payload.get("value"), payload.get("unit")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    factors = {0: 0.0003048, 1: 1, 2: 0.001, 3: 1.609344}
    return int(value * factors[unit]) if unit in factors else None


def _first_int(payload: dict, *names: str) -> int | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _supported_warnings(raw_warnings: object) -> frozenset[str]:
    if isinstance(raw_warnings, dict):
        return frozenset(key for key, active in raw_warnings.items() if active and key in SUPPORTED_WARNINGS)
    if isinstance(raw_warnings, list):
        return frozenset(item for item in raw_warnings if isinstance(item, str) and item in SUPPORTED_WARNINGS)
    return frozenset()
