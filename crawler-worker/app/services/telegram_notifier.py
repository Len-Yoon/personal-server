from __future__ import annotations

import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen


TELEGRAM_API_BASE_URL = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 8
def notify_new_investing_articles(articles: list[dict]) -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return 0

    sent_count = 0
    for article in articles:
        if not _is_alert_article(article):
            continue
        if _send_article(token, chat_id, article):
            sent_count += 1
    return sent_count


def _is_alert_article(article: dict) -> bool:
    relevance = article.get("nasdaq_relevance", {})
    return (
        isinstance(relevance, dict)
        and relevance.get("level") == "alert"
        and bool(relevance.get("reasons"))
    )


def _send_article(token: str, chat_id: str, article: dict) -> bool:
    title = str(article.get("title_ko") or article.get("title") or "새 뉴스").strip()
    url = str(article.get("url") or "").strip()
    relevance = article["nasdaq_relevance"]
    reason = ", ".join(str(item) for item in relevance["reasons"] if str(item).strip())
    if not url:
        return False
    if not reason:
        return False

    source = str(article.get("source") or article.get("provider") or "알 수 없음").strip()
    published_at = str(article.get("published_at") or "알 수 없음").strip()

    payload = {
        "chat_id": chat_id,
        "text": f"[나스닥 중요 알림]\n{title}\n이유: {reason}\n출처: {source} · {published_at}\n{url}",
        "disable_web_page_preview": True,
    }
    request = Request(
        f"{TELEGRAM_API_BASE_URL}/bot{token}/sendMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS):
            return True
    except (OSError, TimeoutError, URLError, ValueError):
        return False
