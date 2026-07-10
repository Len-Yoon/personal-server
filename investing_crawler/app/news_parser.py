from __future__ import annotations

import re
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup, Tag


BASE_URL = "https://kr.investing.com"
TIME_PATTERN = re.compile(r"\d+\s*(?:분|시간|일|주|개월|년)\s*전|오늘|어제")
ARTICLE_PATH_PATTERN = re.compile(r"/news/(?:[^/]+/)?article-\d+")


def parse_news_html(html: str, limit: int = 50) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    articles: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for anchor in soup.select('a[href*="/news/"]'):
        if not isinstance(anchor, Tag):
            continue
        title = _clean_text(anchor.get_text(" ", strip=True))
        url = _normalize_url(anchor.get("href", ""))
        if not title or not url or not ARTICLE_PATH_PATTERN.search(url) or url in seen_urls:
            continue

        container = _find_container(anchor)
        published_at = ""
        published_label = ""
        time_tag = container.select_one("time[datetime]") if container else None
        if time_tag:
            published_at = _clean_text(time_tag.get("datetime", ""))
            published_label = _clean_text(time_tag.get_text(" ", strip=True))
        if not published_label and container:
            published_label = _find_time_label(container.get_text(" ", strip=True))

        source = _find_source(container.get_text(" ", strip=True) if container else "")
        seen_urls.add(url)
        articles.append(
            {
                "title": title,
                "url": url,
                "published_label": published_label,
                "published_at": published_at,
                "source": source or "Investing.com",
            }
        )
        if len(articles) >= limit:
            break

    return articles


def _normalize_url(value: str) -> str:
    if not value:
        return ""
    url = urljoin(BASE_URL, value.strip())
    url, _ = urldefrag(url)
    return url if url.startswith(f"{BASE_URL}/news/") else ""


def _find_container(anchor: Tag) -> Tag:
    article = anchor.find_parent("article")
    if article:
        return article
    current = anchor.parent
    for _ in range(6):
        if isinstance(current, Tag):
            text = current.get_text(" ", strip=True)
            if "By " in text or _find_time_label(text) or current.select_one("time"):
                return current
            current = current.parent
    return anchor.parent if isinstance(anchor.parent, Tag) else anchor


def _find_time_label(text: str) -> str:
    match = TIME_PATTERN.search(text)
    return _clean_text(match.group(0)) if match else ""


def _find_source(text: str) -> str:
    match = re.search(
        r"\bBy\s+(.+?)(?=\s+\d+\s*(?:분|시간|일|주|개월|년)\s*전|\s*[•|]|$)",
        text,
        re.IGNORECASE,
    )
    return _clean_text(match.group(1)) if match else ""


def _clean_text(value: str) -> str:
    return " ".join(value.split())
