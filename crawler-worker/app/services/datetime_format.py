from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo


KOREA_TIMEZONE = ZoneInfo("Asia/Seoul")
KOREAN_WEEKDAYS = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")


def format_news_datetime(value: str) -> str:
    """Return a news timestamp in Korean local time, preserving invalid input."""
    raw = str(value or "").strip()
    if not raw:
        return raw

    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError, OverflowError):
            return raw

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    local = parsed.astimezone(KOREA_TIMEZONE)
    weekday = KOREAN_WEEKDAYS[local.weekday()]
    return f"{local.year}년 {local.month}월 {local.day}일 {weekday} {local:%H:%M:%S}"
