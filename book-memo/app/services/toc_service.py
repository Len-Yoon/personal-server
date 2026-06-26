import html
import re
from typing import Any
from urllib.parse import urljoin

import requests


YES24_SEARCH_URL = "https://www.yes24.com/Product/Search"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.6,en;q=0.5",
}


def fetch_toc_candidates(book: dict[str, Any], limit: int = 120) -> dict[str, Any]:
    candidates = _fetch_yes24_toc(book, limit)

    if candidates:
        return {
            "source": "YES24",
            "candidates": candidates,
        }

    return {
        "source": "",
        "candidates": [],
    }


def _fetch_yes24_toc(book: dict[str, Any], limit: int) -> list[str]:
    query = (book.get("isbn") or "").strip() or (book.get("title") or "").strip()

    if not query:
        return []

    search_response = requests.get(
        YES24_SEARCH_URL,
        params={
            "domain": "ALL",
            "query": query,
        },
        headers=HEADERS,
        timeout=8,
    )
    search_response.raise_for_status()

    detail_url = _extract_yes24_detail_url(search_response.text, book.get("isbn", ""))

    if not detail_url:
        return []

    detail_response = requests.get(detail_url, headers=HEADERS, timeout=8)
    detail_response.raise_for_status()

    toc_html = _extract_yes24_toc_html(detail_response.text)

    if not toc_html:
        return []

    return _normalize_toc_lines(toc_html, limit)


def _extract_yes24_detail_url(page: str, isbn: str = "") -> str:
    isbn = isbn.strip()
    fallback_url = ""

    for match in re.finditer(r"<a\b[^>]*>", page, re.IGNORECASE):
        tag = match.group(0)

        if "gd_name" not in tag:
            continue

        href_match = re.search(r'href=["\']([^"\']+)["\']', tag, re.IGNORECASE)

        if not href_match:
            continue

        detail_url = urljoin("https://www.yes24.com", html.unescape(href_match.group(1)))

        if not fallback_url:
            fallback_url = detail_url

        context = page[max(0, match.start() - 800):match.end() + 800]

        if isbn and isbn in context:
            return detail_url

    if fallback_url:
        return fallback_url

    match = re.search(r'href=["\']([^"\']*/product/goods/\d+)["\']', page, re.IGNORECASE)

    if match:
        return urljoin("https://www.yes24.com", html.unescape(match.group(1)))

    return ""


def _extract_yes24_toc_html(page: str) -> str:
    section_start = page.find('id="infoset_toc"')

    if section_start == -1:
        return ""

    next_section = page.find('id="infoset_', section_start + len('id="infoset_toc"'))
    toc_section = page[section_start:next_section if next_section != -1 else section_start + 50000]
    textarea = re.search(
        r'<textarea[^>]*class="txtContentText"[^>]*>([\s\S]*?)</textarea>',
        toc_section,
        re.IGNORECASE,
    )

    if textarea:
        return textarea.group(1)

    return toc_section


def _normalize_toc_lines(toc_html: str, limit: int) -> list[str]:
    text = html.unescape(toc_html)
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</\s*p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\r", "\n")

    candidates = []
    seen = set()

    for raw_line in text.split("\n"):
        line = _clean_toc_line(raw_line)

        if not _is_useful_toc_line(line) or line in seen:
            continue

        candidates.append(line)
        seen.add(line)

        if len(candidates) >= limit:
            break

    return candidates


def _clean_toc_line(line: str) -> str:
    line = re.sub(r"^\s*[-*•·]\s*", "", line)
    line = re.sub(r"\s+", " ", line)
    return line.strip()


def _is_useful_toc_line(line: str) -> bool:
    if len(line) < 2 or len(line) > 120:
        return False

    if re.fullmatch(r"[\d\s.,~\-–—]+", line):
        return False

    if line in {"목차", "차례", "책소개", "저자 소개", "출판사 리뷰"}:
        return False

    return True
