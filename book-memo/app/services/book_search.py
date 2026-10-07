import asyncio
import copy
import inspect
import json
import os
import time
from collections import OrderedDict
from contextvars import ContextVar
from threading import RLock
from typing import Any

import aiohttp


ALADIN_ITEM_SEARCH_URL = "https://www.aladin.co.kr/ttb/api/ItemSearch.aspx"
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
    aladin_enabled = bool(os.getenv("ALADIN_TTB_KEY", "").strip())
    key = (query, limit, aladin_enabled)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    successful_search = False
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
                    for searcher in (_search_aladin, _search_google_books, _search_open_library):
                        if searcher is _search_aladin and not aladin_enabled:
                            continue
                        try:
                            async with asyncio.timeout(PROVIDER_BUDGET_SECONDS):
                                result = searcher(query, limit)
                                candidate = await result if inspect.isawaitable(result) else result
                                if not isinstance(candidate, list) or any(not isinstance(book, dict) for book in candidate):
                                    raise ValueError("invalid books")
                                books = candidate[:limit]
                        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
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
    if not successful_search:
        raise ValueError("도서 검색 서비스를 이용할 수 없습니다. 잠시 후 다시 검색해주세요.")
    _cache_put(key, books)
    return books


async def _get_json(url, params):
    async with _CLIENT.get().get(url, params=params) as response:
        response.raise_for_status()
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
        return payload


async def _search_aladin(query: str, limit: int) -> list[dict[str, Any]]:
    ttb_key = os.getenv("ALADIN_TTB_KEY", "").strip()

    if not ttb_key:
        return []

    payload = await _get_json(
        ALADIN_ITEM_SEARCH_URL,
        params={
            "ttbkey": ttb_key,
            "Query": query,
            "QueryType": "Keyword",
            "MaxResults": limit,
            "start": 1,
            "SearchTarget": "Book",
            "output": "js",
            "Version": "20131101",
        },
    )

    books = []

    for item in payload.get("item", []):
        isbn = item.get("isbn13") or item.get("isbn") or str(item.get("itemId", ""))
        title = _clean_aladin_title(item.get("title", "제목 없는 책"))

        books.append(
            {
                "external_id": str(item.get("itemId", "")),
                "isbn": isbn,
                "title": title,
                "authors": item.get("author", ""),
                "publisher": item.get("publisher", ""),
                "published_date": item.get("pubDate", ""),
                "description": item.get("description", ""),
                "thumbnail": item.get("cover", "").replace("http://", "https://"),
                "preview_url": item.get("link", ""),
                "source": "aladin",
            }
        )

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


def _clean_aladin_title(title: str) -> str:
    return title.replace(" - ", " - ").strip()
