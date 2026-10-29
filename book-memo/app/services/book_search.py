import asyncio
import copy
import inspect
import hashlib
import html
import json
import time
from collections import OrderedDict
from contextvars import ContextVar
from threading import RLock
from typing import Any

import aiohttp


from app.services.yes24 import api_key, response_data, product_url

YES24_SEARCH_URL = "https://apis.yes24.com/v1/goods/itemList"
GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"
OPEN_LIBRARY_URL = "https://openlibrary.org/search.json"


SEARCH_BUDGET_SECONDS = 3.0
PROVIDER_BUDGET_SECONDS = 1.0
CACHE_TTL_SECONDS = 60.0
CACHE_MAX_ENTRIES = 128
MAX_QUERY_LENGTH = 500
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_CACHE = OrderedDict()
_CACHE_LOCK = RLock()
_CLIENT = ContextVar("book_search_client")


def _cache_get(key):
    now = time.monotonic()
    with _CACHE_LOCK:
        for old_key in [entry for entry, (expires, _) in _CACHE.items() if expires <= now]:
            _CACHE.pop(old_key, None)
        cached = _CACHE.get(key)
        if cached is None:
            return None
        _CACHE.move_to_end(key)
        return copy.deepcopy(cached[1])


def _cache_put(key, books):
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic() + CACHE_TTL_SECONDS, copy.deepcopy(books))
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_MAX_ENTRIES:
            _CACHE.popitem(last=False)


def search_books(query: str, limit: int = 12) -> list[dict[str, Any]]:
    """Synchronous callers use cancellable DNS, avoiding executor shutdown waits."""
    return asyncio.run(search_books_async(query, limit))


async def search_books_async(query: str, limit: int = 12) -> list[dict[str, Any]]:
    query = query.strip()
    if not query:
        return []
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError("검색어는 500자 이내로 입력해주세요.")
    limit = max(1, min(limit, 40))
    yes24_key = api_key()
    yes24_enabled = bool(yes24_key)
    key = (query, limit, hashlib.sha256(yes24_key.encode()).hexdigest())
    cached = _cache_get(key)
    if cached is not None:
        return cached
    successful_search = False
    primary_failed = False
    books = []
    # c-ares DNS is cancellable and does not leave getaddrinfo executor work behind.
    resolver = aiohttp.AsyncResolver()
    try:
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=resolver, limit=3),
            timeout=aiohttp.ClientTimeout(total=SEARCH_BUDGET_SECONDS),
            trust_env=False,
        ) as client:
            context = _CLIENT.set(client)
            try:
                async with asyncio.timeout(SEARCH_BUDGET_SECONDS):
                    for searcher in (_search_yes24, _search_google_books, _search_open_library):
                        if searcher is _search_yes24 and not yes24_enabled:
                            continue
                        try:
                            async with asyncio.timeout(PROVIDER_BUDGET_SECONDS):
                                result = searcher(query, limit)
                                candidate = await result if inspect.isawaitable(result) else result
                                if not isinstance(candidate, list) or any(not isinstance(book, dict) for book in candidate):
                                    raise ValueError("invalid books")
                                books = candidate[:limit]
                        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
                            if searcher is _search_yes24:
                                primary_failed = True
                            continue
                        successful_search = True
                        if books:
                            break
            except TimeoutError:
                pass
            finally:
                _CLIENT.reset(context)
    finally:
        await resolver.close()
    if primary_failed and not books:
        raise ValueError("YES24 도서 검색에 실패했습니다. API 키와 이용 한도를 확인해주세요.")
    if not successful_search:
        raise ValueError("도서 검색 서비스를 이용할 수 없습니다. 잠시 후 다시 검색해주세요.")
    _cache_put(key, books)
    return books


async def _get_json(url, params, headers=None):
    async with _CLIENT.get().get(url, params=params, headers=headers, allow_redirects=False) as response:
        if response.status != 200 and not (url == YES24_SEARCH_URL and response.status == 404):
            raise ValueError("도서 검색 제공자 응답 오류")
        size = 0
        chunks = []
        async for chunk in response.content.iter_chunked(16384):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("검색 응답이 너무 큽니다.")
            chunks.append(chunk)
        payload = json.loads(b"".join(chunks))
        if not isinstance(payload, dict):
            raise ValueError("검색 응답 형식이 올바르지 않습니다.")
        if response.status == 404:
            if payload.get("success") is False and payload.get("errorCode") == "SEARCH_001":
                return {"success": True, "errorCode": None, "data": {"items": []}}
            raise ValueError("YES24 검색 제공자 오류")
        return payload


async def _search_yes24(query: str, limit: int) -> list[dict[str, Any]]:
    key = api_key()
    if not key:
        return []
    payload = await _get_json(
        YES24_SEARCH_URL,
        params={"query": query, "category": "BOOK", "page": 1, "pageSize": limit},
        headers={"X-Api-Key": key},
    )
    data = response_data(payload)
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError("YES24 검색 응답 형식 오류")
    books = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("YES24 검색 항목 형식 오류")
        item_id = str(item.get("itemId") or "")
        isbn = str(item.get("isbn13") or item.get("isbn10") or "").strip()
        if not item_id.isdigit() or not isbn or not item.get("title"):
            raise ValueError("YES24 검색 필수 정보 누락")
        detail = item.get("contentDetail") or {}
        if not isinstance(detail, dict):
            raise ValueError("YES24 도서 소개 형식 오류")
        books.append({
            "external_id": item_id, "isbn": isbn, "title": item["title"],
            "authors": item.get("author") or "", "publisher": item.get("publisher") or "",
            "published_date": item.get("publishDate") or "",
            "description": html.unescape(str(detail.get("bookIntroduction") or "")),
            "thumbnail": str(item.get("cover") or "").replace("http://", "https://"),
            "preview_url": product_url(item_id), "source": "yes24",
        })
    return books


async def _search_google_books(query: str, limit: int) -> list[dict[str, Any]]:
    payload = await _get_json(
        GOOGLE_BOOKS_URL,
        params={
            "q": query,
            "maxResults": limit,
            "printType": "books",
            "langRestrict": "ko",
        },
    )

    books = []

    for item in payload.get("items", []):
        volume = item.get("volumeInfo", {})
        image_links = volume.get("imageLinks", {})
        industry_ids = volume.get("industryIdentifiers", [])
        isbn = _extract_isbn(industry_ids) or item.get("id", "")
        title = volume.get("title", "제목 없는 책")
        authors = volume.get("authors", [])

        books.append(
            {
                "external_id": item.get("id", ""),
                "isbn": isbn,
                "title": title,
                "authors": ", ".join(authors),
                "publisher": volume.get("publisher", ""),
                "published_date": volume.get("publishedDate", ""),
                "description": volume.get("description", ""),
                "thumbnail": image_links.get("thumbnail", "").replace("http://", "https://"),
                "preview_url": volume.get("previewLink", ""),
                "source": "google_books",
            }
        )

    return books


async def _search_open_library(query: str, limit: int) -> list[dict[str, Any]]:
    payload = await _get_json(
        OPEN_LIBRARY_URL,
        params={
            "q": query,
            "limit": limit,
        },
    )

    books = []

    for item in payload.get("docs", []):
        isbn_values = item.get("isbn") or []
        isbn = isbn_values[0] if isbn_values else item.get("key", "")
        cover_id = item.get("cover_i")
        thumbnail = ""

        if cover_id:
            thumbnail = f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg"

        books.append(
            {
                "external_id": item.get("key", ""),
                "isbn": isbn,
                "title": item.get("title", "제목 없는 책"),
                "authors": ", ".join(item.get("author_name", [])),
                "publisher": ", ".join((item.get("publisher") or [])[:2]),
                "published_date": str(item.get("first_publish_year", "")),
                "description": "",
                "thumbnail": thumbnail,
                "preview_url": f"https://openlibrary.org{item.get('key', '')}",
                "source": "open_library",
            }
        )

    return books


def _extract_isbn(industry_ids: list[dict[str, str]]) -> str:
    for item in industry_ids:
        if item.get("type") == "ISBN_13":
            return item.get("identifier", "")

    for item in industry_ids:
        if item.get("type") == "ISBN_10":
            return item.get("identifier", "")

    return ""
