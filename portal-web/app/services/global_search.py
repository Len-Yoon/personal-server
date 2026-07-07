import json
import os
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen


DEFAULT_ENDPOINTS = {
    "news": "http://crawler-worker:8001/api/search",
    "youtube": "http://youtube-memo:8002/api/search",
    "books": "http://book-memo:8003/api/search",
}
DEFAULT_PUBLIC_URLS = {
    "news": "https://news.len.pe.kr",
    "youtube": "https://memo.len.pe.kr",
    "books": "https://books.len.pe.kr",
}
PUBLIC_URL_ENVS = {
    "news": "NEWS_SERVICE_URL",
    "youtube": "YOUTUBE_MEMO_URL",
    "books": "BOOK_MEMO_URL",
}


def search_all(query: str, limit: int = 5) -> dict[str, list[dict[str, Any]]]:
    query = query.strip()
    if not query:
        return {"news": [], "youtube": [], "books": []}

    if _truthy(os.getenv("DEMO_MODE", "")):
        return _demo_results(query)

    return {
        name: _fetch_results(name, _endpoint(name), query, limit)
        for name in ("news", "youtube", "books")
    }


def _endpoint(name: str) -> str:
    env_name = f"{name.upper()}_SEARCH_URL"
    return os.getenv(env_name, DEFAULT_ENDPOINTS[name])


def _fetch_results(name: str, endpoint: str, query: str, limit: int) -> list[dict[str, Any]]:
    url = f"{endpoint}?{urlencode({'q': query, 'limit': limit})}"
    try:
        with urlopen(url, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return []
    results = payload.get("results", [])
    if not isinstance(results, list):
        return []
    return [_normalize_result_url(name, item) for item in results if isinstance(item, dict)]


def _normalize_result_url(name: str, item: dict[str, Any]) -> dict[str, Any]:
    url = str(item.get("url", "#"))
    if url.startswith("/"):
        base_url = os.getenv(PUBLIC_URL_ENVS[name], DEFAULT_PUBLIC_URLS[name]).rstrip("/")
        item = dict(item)
        item["url"] = f"{base_url}{url}"
    return item


def _demo_results(query: str) -> dict[str, list[dict[str, Any]]]:
    return {
        "news": [
            {
                "title": f"{query} 관련 저장 뉴스",
                "description": "DEMO_MODE 샘플 뉴스 요약입니다.",
                "snippet": "시장 흐름과 주요 이슈를 짧게 정리한 공개용 샘플입니다.",
                "meta": "뉴스 · 샘플 · 오늘",
                "url": "#",
            }
        ],
        "youtube": [
            {
                "title": f"{query} 학습 영상",
                "description": "샘플 유튜브 메모 2개",
                "snippet": "영상에서 다시 볼 부분과 핵심 메모를 함께 보여주는 예시입니다.",
                "meta": "유튜브 · 메모 2개 · 샘플",
                "url": "#",
            }
        ],
        "books": [
            {
                "title": f"{query} 독서 메모",
                "description": "샘플 책 진행률 64%",
                "snippet": "목차별 진행률과 독서 메모 일부를 보여주는 공개용 샘플입니다.",
                "meta": "책 · 진행률 64% · 샘플",
                "url": "#",
            }
        ],
    }


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
