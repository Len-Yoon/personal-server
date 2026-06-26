import os
from typing import Any

import requests


ALADIN_ITEM_SEARCH_URL = "https://www.aladin.co.kr/ttb/api/ItemSearch.aspx"
GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"
OPEN_LIBRARY_URL = "https://openlibrary.org/search.json"


def search_books(query: str, limit: int = 12) -> list[dict[str, Any]]:
    query = query.strip()

    if not query:
        return []

    for searcher in (_search_aladin, _search_google_books, _search_open_library):
        try:
            books = searcher(query, limit)
        except requests.RequestException:
            continue

        if books:
            return books

    return []


def _search_aladin(query: str, limit: int) -> list[dict[str, Any]]:
    ttb_key = os.getenv("ALADIN_TTB_KEY", "").strip()

    if not ttb_key:
        return []

    response = requests.get(
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
        timeout=8,
    )
    response.raise_for_status()

    books = []

    for item in response.json().get("item", []):
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


def _search_google_books(query: str, limit: int) -> list[dict[str, Any]]:
    response = requests.get(
        GOOGLE_BOOKS_URL,
        params={
            "q": query,
            "maxResults": limit,
            "printType": "books",
            "langRestrict": "ko",
        },
        timeout=8,
    )
    response.raise_for_status()

    books = []

    for item in response.json().get("items", []):
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


def _search_open_library(query: str, limit: int) -> list[dict[str, Any]]:
    response = requests.get(
        OPEN_LIBRARY_URL,
        params={
            "q": query,
            "limit": limit,
        },
        timeout=8,
    )
    response.raise_for_status()

    books = []

    for item in response.json().get("docs", []):
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
