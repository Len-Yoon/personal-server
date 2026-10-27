import asyncio
import os
from collections import Counter
from threading import RLock
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

DEFAULT_ENDPOINTS = {
    "news": "http://crawler-worker:8001/api/search",
    "youtube": "http://youtube-memo:8002/api/search",
    "books": "http://book-memo:8003/api/search",
}
SEARCH_BUDGET_SECONDS = 1.5
SEARCH_SOURCES = ("news", "youtube", "books")
SOURCE_OUTCOMES = (
    "ok", "timeout", "http_error", "invalid_response", "transport_error", "internal_error", "cancelled",
)
REQUEST_OUTCOMES = ("complete", "partial", "failed", "cancelled")


class SearchMetrics:
    """Process-local counters; stdlib-only to retain standalone lab loading."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._sources: Counter[tuple[str, str]] = Counter({
            (source, outcome): 0
            for source in SEARCH_SOURCES for outcome in SOURCE_OUTCOMES
        })
        self._requests: Counter[str] = Counter({outcome: 0 for outcome in REQUEST_OUTCOMES})

    def record(self, outcomes: dict[str, str], *, cancelled: bool = False) -> None:
        if not outcomes or any(
            source not in SEARCH_SOURCES or outcome not in SOURCE_OUTCOMES
            for source, outcome in outcomes.items()
        ):
            raise ValueError("invalid search metric labels")
        successful = sum(value == "ok" for value in outcomes.values())
        outcome = "complete" if successful == len(outcomes) else "partial" if successful else "failed"
        if cancelled:
            outcome = "cancelled"
        with self._lock:
            self._requests[outcome] += 1
            for source, result in outcomes.items():
                self._sources[(source, result)] += 1

    def render(self) -> str:
        with self._lock:
            requests = dict(self._requests)
            sources = dict(self._sources)
        lines = [
            "# HELP portal_search_requests_total Real nonblank search executions by completeness of requested sources.",
            "# TYPE portal_search_requests_total counter",
        ]
        for outcome, count in sorted(requests.items()):
            lines.append(f'portal_search_requests_total{{outcome="{outcome}"}} {count}')
        lines.extend([
            "# HELP portal_search_source_requests_total Requested search source outcomes, including deadline timeouts.",
            "# TYPE portal_search_source_requests_total counter",
        ])
        for (source, outcome), count in sorted(sources.items()):
            lines.append(
                f'portal_search_source_requests_total{{outcome="{outcome}",source="{source}"}} {count}'
            )
        return "\n".join(lines) + "\n"


SEARCH_METRICS = SearchMetrics()


async def search_all(
    query: str,
    limit: int = 5,
    public_base_urls: dict[str, str] | None = None,
    local_base_urls: dict[str, str] | None = None,
    prefer_local: bool = False,
    source: str = "all",
) -> dict[str, dict[str, object]]:
    query = query.strip()
    sources = (source,) if source in SEARCH_SOURCES else SEARCH_SOURCES
    if not query:
        return {name: {"items": [], "status": "ok"} for name in sources}

    if _truthy(os.getenv("DEMO_MODE", "")):
        demo = _demo_results(query)
        return {name: demo[name] for name in sources}

    return await _search_parallel(
        query, limit, public_base_urls, local_base_urls, prefer_local, sources
    )


async def _search_parallel(
    query: str,
    limit: int,
    public_base_urls: dict[str, str] | None,
    local_base_urls: dict[str, str] | None,
    prefer_local: bool,
    sources: tuple[str, ...],
) -> dict[str, dict[str, object]]:
    results = {name: {"items": [], "status": "unavailable"} for name in sources}
    outcomes = {name: "internal_error" for name in sources}
    cancelled = False
    try:
        async with httpx.AsyncClient(timeout=SEARCH_BUDGET_SECONDS, follow_redirects=True) as client:
            tasks = {
                name: asyncio.create_task(_fetch_results(
                    client, name, _endpoint(name), query, limit,
                    public_base_urls, local_base_urls, prefer_local,
                ))
                for name in sources
            }
            try:
                await asyncio.wait(tasks.values(), timeout=SEARCH_BUDGET_SECONDS)
            except asyncio.CancelledError:
                cancelled = True
                raise
            finally:
                # Freeze outcomes before draining: a late result after cancellation
                # must not turn an expired request into a successful observation.
                for name, task in tasks.items():
                    if not task.done() or task.cancelled():
                        outcomes[name] = "cancelled" if cancelled else "timeout"
                        task.cancel()
                    else:
                        try:
                            results[name], outcomes[name] = task.result()
                        except Exception:
                            outcomes[name] = "internal_error"
                await asyncio.gather(*tasks.values(), return_exceptions=True)
    except asyncio.CancelledError:
        cancelled = True
        raise
    except Exception:
        # Client setup/teardown failures retain the public degraded-result contract.
        pass
    finally:
        SEARCH_METRICS.record(outcomes, cancelled=cancelled)
    return results


def _endpoint(name: str) -> str:
    env_name = f"{name.upper()}_SEARCH_URL"
    return os.getenv(env_name, DEFAULT_ENDPOINTS[name])


async def _fetch_results(
    client: httpx.AsyncClient,
    name: str,
    endpoint: str,
    query: str,
    limit: int,
    public_base_urls: dict[str, str] | None = None,
    local_base_urls: dict[str, str] | None = None,
    prefer_local: bool = False,
) -> tuple[dict[str, object], str]:
    url = f"{endpoint}?{urlencode({'q': query, 'limit': limit})}"
    try:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("invalid search payload")
        results = payload.get("results")
        if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
            raise ValueError("invalid search results")
        return {
            "items": [
                _normalize_result_url(
                    name,
                    item,
                    public_base_urls=public_base_urls,
                    local_base_urls=local_base_urls,
                    prefer_local=prefer_local,
                )
                for item in results
            ],
            "status": "ok",
        }, "ok"
    except httpx.TimeoutException:
        outcome = "timeout"
    except httpx.HTTPStatusError:
        outcome = "http_error"
    except httpx.RequestError:
        outcome = "transport_error"
    except ValueError:
        outcome = "invalid_response"
    except Exception:
        outcome = "internal_error"
    return {"items": [], "status": "unavailable"}, outcome


def _normalize_result_url(
    name: str,
    item: dict[str, Any],
    public_base_urls: dict[str, str] | None = None,
    local_base_urls: dict[str, str] | None = None,
    prefer_local: bool = False,
) -> dict[str, Any]:
    url = str(item.get("url", "#"))
    if url == "#":
        return item

    if url.startswith("/") and not url.startswith("//"):
        base_urls = local_base_urls if prefer_local and local_base_urls else public_base_urls
        if not base_urls:
            base_urls = {
                "news": "https://news.len.pe.kr",
                "youtube": "https://memo.len.pe.kr",
                "books": "https://books.len.pe.kr",
            }
        base_url = base_urls[name].rstrip("/")
        item = dict(item)
        item["url"] = f"{base_url}{url}"
        return item

    parsed_url = urlsplit(url)
    if parsed_url.scheme == "https" and parsed_url.netloc:
        return item

    item = dict(item)
    item["url"] = "#"
    return item


def _demo_results(query: str) -> dict[str, dict[str, object]]:
    return {
        "news": {
            "items": [
                {
                    "title": f"{query} 관련 저장 뉴스",
                    "description": "DEMO_MODE 샘플 뉴스 요약입니다.",
                    "snippet": "시장 흐름과 주요 이슈를 짧게 정리한 공개용 샘플입니다.",
                    "meta": "뉴스 · 샘플 · 오늘",
                    "url": "#",
                }
            ],
            "status": "ok",
        },
        "youtube": {
            "items": [
                {
                    "title": f"{query} 학습 영상",
                    "description": "샘플 유튜브 메모 2개",
                    "snippet": "영상에서 다시 볼 부분과 핵심 메모를 함께 보여주는 예시입니다.",
                    "meta": "유튜브 · 메모 2개 · 샘플",
                    "url": "#",
                }
            ],
            "status": "ok",
        },
        "books": {
            "items": [
                {
                    "title": f"{query} 독서 메모",
                    "description": "샘플 책 진행률 64%",
                    "snippet": "목차별 진행률과 독서 메모 일부를 보여주는 공개용 샘플입니다.",
                    "meta": "책 · 진행률 64% · 샘플",
                    "url": "#",
                }
            ],
            "status": "ok",
        },
    }


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
