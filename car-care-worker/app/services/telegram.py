from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen

from app.models import VehicleSnapshot
from app.services.maintenance import MAINTENANCE_RULES
from app.services.store import CarCareStore


TELEGRAM_API_BASE_URL = "https://api.telegram.org"


@dataclass(frozen=True)
class TelegramUpdate:
    chat_id: str
    text: str
    update_id: int | None = None


class TelegramClient:
    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = str(chat_id)

    @classmethod
    def from_environment(cls) -> "TelegramClient | None":
        token = os.getenv("CAR_CARE_TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("CAR_CARE_TELEGRAM_CHAT_ID", "")
        return cls(token, chat_id) if token and chat_id else None

    def poll(self, offset: int | None) -> list[TelegramUpdate]:
        payload: dict[str, int] = {"timeout": 25}
        if offset is not None:
            payload["offset"] = offset
        data = self._post("getUpdates", payload, timeout=30)
        if not data or not data.get("ok"):
            return []
        result = data.get("result")
        if not isinstance(result, list):
            return []
        updates: list[TelegramUpdate] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            message = item.get("message", {})
            if not isinstance(message, dict):
                continue
            text = message.get("text")
            chat = message.get("chat", {})
            if not isinstance(chat, dict):
                continue
            chat_id = chat.get("id")
            if isinstance(text, str) and chat_id is not None:
                updates.append(TelegramUpdate(str(chat_id), text, item.get("update_id")))
        return updates

    def send(self, text: str) -> bool:
        data = self._post("sendMessage", {"chat_id": self._chat_id, "text": text}, timeout=8)
        return bool(data and data.get("ok"))

    def _post(self, method: str, payload: dict[str, object], timeout: int) -> dict | None:
        request = Request(
            f"{TELEGRAM_API_BASE_URL}/bot{self._token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, ValueError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None


class CommandHandler:
    _ITEM_ALIASES = {"엔진오일": "engine_oil", "미션오일": "transmission_oil"}

    def __init__(self, store: CarCareStore, allowed_chat_id: str) -> None:
        self.store = store
        self._allowed_chat_id = str(allowed_chat_id)

    def handle_update(self, update: TelegramUpdate) -> str | None:
        if update.chat_id != self._allowed_chat_id:
            return None
        parts = update.text.strip().split()
        if not parts:
            return self._usage()
        command = parts[0].split("@", 1)[0]
        if command == "/차량" and len(parts) == 1:
            return self._vehicle_status()
        if command == "/주행거리":
            return self._set_odometer(parts)
        if command == "/정비완료":
            return self._complete_maintenance(parts)
        if command == "/정비목록" and len(parts) == 1:
            return self._maintenance_list()
        if command == "/알림테스트" and len(parts) == 1:
            return "알림 테스트: 차량관리 Bot 명령 수신이 정상입니다."
        return self._usage()

    def _vehicle_status(self) -> str:
        snapshot = self.store.load_last_snapshot()
        if snapshot is None:
            return "차량 상태: 수동 모드\n등록된 주행거리가 없습니다. /주행거리 <km>로 등록하세요."
        details = [f"누적 주행거리: {snapshot.odometer_km:,}km"]
        if snapshot.dte_km is not None:
            details.append(f"주행 가능 거리: {snapshot.dte_km:,}km")
        else:
            details.append("주행 가능 거리: 확인 필요")
        details.append("상태 입력: 수동 모드")
        return "\n".join(details)

    def _set_odometer(self, parts: list[str]) -> str:
        if len(parts) != 2:
            return self._usage()
        odometer_km = self._parse_odometer(parts[1])
        if odometer_km is None:
            return self._usage()
        previous = self.store.load_last_snapshot()
        self.store.save_snapshot(
            VehicleSnapshot(
                observed_at=datetime.now(timezone.utc),
                odometer_km=odometer_km,
                dte_km=None if previous is None else previous.dte_km,
                warnings=frozenset() if previous is None else previous.warnings,
            )
        )
        return f"주행거리 등록 완료: {odometer_km:,}km"

    def _complete_maintenance(self, parts: list[str]) -> str:
        if len(parts) not in (2, 3):
            return self._usage()
        item = self._ITEM_ALIASES.get(parts[1])
        if item is None:
            return self._usage()
        odometer_km = self._parse_odometer(parts[2]) if len(parts) == 3 else self._current_odometer()
        if len(parts) == 3 and odometer_km is None:
            return self._usage()
        self.store.complete_maintenance(item, odometer_km, date.today())
        item_name = parts[1]
        odometer_text = "주행거리 미입력" if odometer_km is None else f"{odometer_km:,}km"
        return f"{item_name} 정비 완료: {odometer_text}"

    def _current_odometer(self) -> int | None:
        snapshot = self.store.load_last_snapshot()
        return None if snapshot is None else snapshot.odometer_km

    @staticmethod
    def _parse_odometer(value: str) -> int | None:
        try:
            odometer_km = int(value)
        except ValueError:
            return None
        return odometer_km if odometer_km >= 0 else None

    @staticmethod
    def _maintenance_list() -> str:
        engine = MAINTENANCE_RULES["engine_oil"]
        transmission = MAINTENANCE_RULES["transmission_oil"]
        return (
            f"엔진오일: {engine.interval_km:,}km 또는 {engine.interval_months}개월 "
            f"(사전 알림 {engine.warning_km:,}km/{engine.warning_days}일)\n"
            f"미션오일: {transmission.interval_km:,}km "
            f"(사전 알림 {transmission.warning_km:,}km)"
        )

    @staticmethod
    def _usage() -> str:
        return (
            "사용법:\n/차량\n/주행거리 <km>\n"
            "/정비완료 엔진오일 [km]\n/정비완료 미션오일 [km]\n/정비목록\n/알림테스트"
        )
