from typing import Any


def notification_articles(value: Any) -> list[dict[str, Any]]:
    return [dict(article) for article in value if isinstance(article, dict)] if isinstance(value, list) else []


def notification_times(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(timestamp) for key, timestamp in value.items()}
