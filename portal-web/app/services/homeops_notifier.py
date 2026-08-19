from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


TELEGRAM_API_BASE_URL = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 8


class HomeOpsTelegramNotifier:
    """Best-effort alert delivery that never blocks HomeOps recovery."""

    def send(self, event_type: str, details: dict[str, Any]) -> bool:
        token = os.getenv("HOMEOPS_TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("HOMEOPS_TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            return False
        payload = {
            "chat_id": chat_id,
            "text": self._message(event_type, details),
            "disable_web_page_preview": True,
        }
        request = Request(
            f"{TELEGRAM_API_BASE_URL}/bot{token}/sendMessage",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS):
                return True
        except (OSError, TimeoutError, URLError, ValueError):
            return False

    @staticmethod
    def _message(event_type: str, details: dict[str, Any]) -> str:
        labels = {
            "container_restart_started": "컨테이너 재시작 시작",
            "container_recovery_verified": "컨테이너 복구 확인",
            "container_recovery_failed": "컨테이너 복구 실패",
            "host_memory_high": "호스트 메모리 경고",
            "host_memory_recovered": "호스트 메모리 정상화",
        }
        lines = [f"[HomeOps] {labels.get(event_type, '운영 알림')}"]
        if details.get("service"):
            lines.append(f"서비스: {details['service']}")
        if details.get("reason"):
            lines.append(f"사유: {details['reason']}")
        if details.get("memory_percent") is not None:
            lines.append(f"호스트 메모리: {float(details['memory_percent']):.1f}%")
        if details.get("admin_url"):
            lines.append(f"관리자: {details['admin_url']}")
        return "\n".join(lines)
