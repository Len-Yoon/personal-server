from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo


KOREA_TIMEZONE = ZoneInfo("Asia/Seoul")


def format_news_datetime(value: str | None) -> str:
    """Return a valid news timestamp in Korean local time for display."""
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
            return ""

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    local = parsed.astimezone(KOREA_TIMEZONE)
    return local.strftime("%Y-%m-%d %H:%M")
