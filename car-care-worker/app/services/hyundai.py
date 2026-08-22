from datetime import datetime, timezone
import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen

from app.models import VehicleSnapshot
from app.services.vehicle_monitor import SUPPORTED_WARNINGS


class HyundaiClient:
    _REQUIRED_ENVIRONMENT = (
        "HYUNDAI_CLIENT_ID",
        "HYUNDAI_CLIENT_SECRET",
        "HYUNDAI_ACCESS_TOKEN",
        "HYUNDAI_VEHICLE_ID",
        "HYUNDAI_API_URL",
    )

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        access_token: str | None = None,
        vehicle_id: str | None = None,
        api_url: str | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token = access_token
        self._vehicle_id = vehicle_id
        self._api_url = api_url

    @classmethod
    def from_environment(cls) -> "HyundaiClient":
        return cls(*(os.getenv(name) for name in cls._REQUIRED_ENVIRONMENT))

    def fetch_snapshot(self) -> VehicleSnapshot | None:
        if not all((self._client_id, self._client_secret, self._access_token, self._vehicle_id, self._api_url)):
            return None
        request = Request(
            self._api_url,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "X-Client-Id": self._client_id,
                "X-Client-Secret": self._client_secret,
                "X-Vehicle-Id": self._vehicle_id,
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, ValueError, json.JSONDecodeError):
            return None
        return self._to_snapshot(payload)

    @staticmethod
    def _to_snapshot(payload: object) -> VehicleSnapshot | None:
        if not isinstance(payload, dict):
            return None
        odometer = _first_int(payload, "odometer_km", "odometer", "mileage")
        if odometer is None or odometer < 0:
            return None
        dte = _first_int(payload, "dte_km", "dte", "driving_range")
        warnings = _supported_warnings(payload.get("warnings", []))
        return VehicleSnapshot(datetime.now(timezone.utc), odometer, dte, warnings)


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
