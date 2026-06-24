from datetime import datetime
from urllib.parse import urlencode

import requests


GDELT_DOC_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def search_gdelt_articles(query: str, category: str, limit: int = 20):
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": min(limit, 50),
        "sort": "datedesc",
    }

    url = f"{GDELT_DOC_API_URL}?{urlencode(params)}"

    try:
        response = requests.get(url, timeout=12)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        return [
            {
                "category": category,
                "title": "GDELT 수집 실패",
                "url": "",
                "source": "GDELT",
                "published_at": "",
                "summary": str(e),
                "provider": "GDELT",
            }
        ]

    articles = []

    for item in data.get("articles", []):
        title = item.get("title")
        article_url = item.get("url")
        domain = item.get("domain") or item.get("sourceCountry") or "unknown"
        published_at = item.get("seendate") or ""

        if not title or not article_url:
            continue

        articles.append(
            {
                "category": category,
                "title": title,
                "url": article_url,
                "source": domain,
                "published_at": _format_gdelt_date(published_at),
                "summary": "",
                "provider": "GDELT",
            }
        )

    return articles


def _format_gdelt_date(value: str):
    if not value:
        return ""

    try:
        dt = datetime.strptime(value[:14], "%Y%m%d%H%M%S")
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value