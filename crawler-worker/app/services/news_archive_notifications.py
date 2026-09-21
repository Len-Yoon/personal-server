import hashlib
from typing import Any


def notification_articles(value: Any) -> list[dict[str, Any]]:
    return [dict(article) for article in value if isinstance(article, dict)] if isinstance(value, list) else []


def notification_times(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(timestamp) for key, timestamp in value.items()}


def notification_outbox(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for event in value:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("event_id", "")).strip()
        kind = str(event.get("kind", "")).strip()
        status = str(event.get("status", "")).strip()
        articles = notification_articles(event.get("articles", []))
        urls = [str(article.get("url", "")).strip() for article in articles]
        if not event_id or event_id in seen_ids or kind not in {"alert", "digest"}:
            continue
        if status not in {"pending", "sent"} or not articles or not all(urls):
            continue
        normalized_event = {
            "event_id": event_id,
            "kind": kind,
            "status": status,
            "articles": articles,
            "article_urls": urls,
            "created_at": str(event.get("created_at", "")),
        }
        removed_urls = event.get("removed_urls", urls)
        if isinstance(removed_urls, list):
            normalized_event["removed_urls"] = [str(url).strip() for url in removed_urls if str(url).strip()]
        if status == "sent":
            normalized_event["sent_at"] = str(event.get("sent_at", ""))
        normalized.append(normalized_event)
        seen_ids.add(event_id)
    return normalized


def notification_event(kind: str, articles: list[dict[str, Any]], created_at: str) -> dict[str, Any]:
    urls = sorted(str(article["url"]).strip() for article in articles)
    event_hash = hashlib.sha256("\n".join(urls).encode("utf-8")).hexdigest()
    return {
        "event_id": f"{kind}:{event_hash}",
        "kind": kind,
        "status": "pending",
        "articles": [dict(article) for article in articles],
        "article_urls": urls,
        "created_at": created_at,
    }
