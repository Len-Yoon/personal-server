from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.request import Request, urlopen


VELOG_TRENDING_URLS = (
    "https://velog.io/trending/week",
    "https://beta.velog.io/trending/week",
)
POST_URL_PATTERN = re.compile(r"^https?://(?:www\.)?velog\.io/@[^/]+/[^/?#]+$")


class _VelogLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.posts: list[tuple[str, str]] = []
        self._current_url = ""
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" or self._current_url:
            return
        attributes = dict(attrs)
        url = str(attributes.get("href") or "")
        if url.startswith("/"):
            url = f"https://velog.io{url}"
        if POST_URL_PATTERN.match(url):
            self._current_url = url
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_url:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._current_url:
            return
        title = " ".join("".join(self._current_text).split())
        if title:
            self.posts.append((self._current_url, title))
        self._current_url = ""
        self._current_text = []


def search_velog_trending(limit: int = 20) -> list[dict]:
    for url in VELOG_TRENDING_URLS:
        html = _fetch(url)
        if not html:
            continue
        posts = _VelogLinkParser()
        posts.feed(html)
        articles = _to_articles(posts.posts, limit)
        if articles:
            return articles
    return []


def _fetch(url: str) -> str:
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "personal-server-news/1.0",
        },
    )
    try:
        with urlopen(request, timeout=8) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _to_articles(posts: list[tuple[str, str]], limit: int) -> list[dict]:
    articles: list[dict] = []
    seen: set[str] = set()
    for url, title in posts:
        if url in seen:
            continue
        seen.add(url)
        articles.append(
            {
                "category": "KR_STACK",
                "title": title,
                "title_ko": title,
                "title_original": title,
                "url": url,
                "source": "Velog",
                "provider": "Velog Trending",
                "published_at": "",
                "published_at_sort": "",
                "summary": "벨로그 주간 트렌딩 개발 글",
                "topics": ["개발 트렌드"],
                "source_status": "velog",
            }
        )
        if len(articles) >= limit:
            break
    return articles
