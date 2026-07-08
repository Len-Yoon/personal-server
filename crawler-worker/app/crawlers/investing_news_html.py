from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.error import URLError
from urllib.request import Request, urlopen


BASE_URL = "https://kr.investing.com"
REQUEST_TIMEOUT_SECONDS = 12

INVESTING_CATEGORY_URLS = {
    "WORLD": [
        f"{BASE_URL}/news/economy",
        f"{BASE_URL}/news/world-news",
    ],
    "NASDAQ": [
        f"{BASE_URL}/news/stock-market-news",
    ],
    "GOLD": [
        f"{BASE_URL}/news/commodities-news",
    ],
    "HK50": [
        f"{BASE_URL}/news/stock-market-news",
    ],
}

ARTICLE_PATH_RE = re.compile(r"^/news/(?!$)(?![^/]+/?$).+")
BYLINE_RE = re.compile(r"^By\s*(?P<source>[^•]+)•(?P<published>.+)$")
SKIP_TEXTS = {
    "광고",
    "더보기",
    "최신",
    "많이 본",
    "뉴스 속보",
    "경제",
    "주식 시장",
    "상품과 선물",
    "외환",
    "암호화폐",
    "일반",
}


def search_investing_news_html(
    category: str,
    limit: int = 20,
    source_filter: str = "",
) -> list[dict]:
    articles = []

    for url in INVESTING_CATEGORY_URLS.get(category, INVESTING_CATEGORY_URLS["WORLD"]):
        try:
            html = _fetch_html(url)
        except (TimeoutError, OSError, URLError):
            continue

        if _is_cloudflare_challenge(html):
            continue

        articles.extend(parse_investing_news_html(html, category=category, page_url=url))

        if len(articles) >= limit:
            break

    if source_filter:
        normalized_filter = source_filter.casefold()
        articles = [
            article
            for article in articles
            if normalized_filter in article.get("source", "").casefold()
        ]

    return _dedupe_articles(articles)[:limit]


def _fetch_html(url: str) -> str:
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        },
    )

    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def _is_cloudflare_challenge(html: str) -> bool:
    lowered = html.lower()
    return "just a moment" in lowered and "challenge-platform" in lowered


def parse_investing_news_html(html: str, category: str, page_url: str) -> list[dict]:
    parser = _NewsListParser()
    parser.feed(html)
    nodes = parser.nodes
    start_index = _content_start_index(nodes)
    articles = []

    index = start_index
    while index < len(nodes):
        node = nodes[index]

        if node["type"] != "link" or not _is_article_link(node.get("href", "")):
            index += 1
            continue

        title = _clean_text(node.get("text", ""))

        if not _looks_like_title(title):
            index += 1
            continue

        details, index = _collect_article_details(nodes, index + 1)
        source = details["source"] or "Investing.com"
        published_at = details["published_at"]
        summary = details["summary"]

        articles.append(
            {
                "category": category,
                "title": title,
                "title_ko": title,
                "title_original": title,
                "url": urljoin(page_url, node["href"]),
                "source": source,
                "published_at": published_at,
                "published_at_sort": "",
                "summary": summary,
                "provider": "Investing.com KR",
            }
        )

    return articles


def _content_start_index(nodes: list[dict]) -> int:
    for index, node in enumerate(nodes):
        if node["type"] == "text" and node["text"].startswith("# "):
            return index + 1

    for index, node in enumerate(nodes):
        if node["type"] == "text" and node["text"] in {"주식 시장 뉴스", "상품과 선물 뉴스", "경제 뉴스"}:
            return index + 1

    return 0


def _collect_article_details(nodes: list[dict], index: int) -> tuple[dict, int]:
    summary = ""
    source = ""
    published_at = ""

    while index < len(nodes):
        node = nodes[index]

        if node["type"] == "link" and _is_article_link(node.get("href", "")):
            break

        text = _clean_text(node.get("text", ""))
        byline = BYLINE_RE.match(text)

        if byline:
            source = _clean_text(byline.group("source"))
            published_at = _clean_text(byline.group("published"))
            index += 1
            break

        if not summary and _looks_like_summary(text):
            summary = text

        index += 1

    return {"summary": summary, "source": source, "published_at": published_at}, index


def _is_article_link(href: str) -> bool:
    if not href:
        return False

    parsed = urlparse(href)
    path = parsed.path if parsed.scheme else href.split("?", 1)[0]
    return bool(ARTICLE_PATH_RE.match(path))


def _looks_like_title(text: str) -> bool:
    if len(text) < 8:
        return False

    if text in SKIP_TEXTS:
        return False

    return not text.startswith(("http://", "https://"))


def _looks_like_summary(text: str) -> bool:
    if len(text) < 20:
        return False

    if text in SKIP_TEXTS:
        return False

    return not BYLINE_RE.match(text)


def _clean_text(value: str) -> str:
    value = unescape(value or "")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _dedupe_articles(articles: list[dict]) -> list[dict]:
    seen = set()
    deduped = []

    for article in articles:
        url = article.get("url")

        if not url or url in seen:
            continue

        seen.add(url)
        deduped.append(article)

    return deduped


class _NewsListParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.nodes = []
        self._link_stack = []
        self._heading_stack = []
        self._current_link = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)

        if tag == "a":
            self._current_link = {
                "href": attrs_dict.get("href", ""),
                "text": [],
            }
            self._link_stack.append(self._current_link)

        if tag in {"h1", "h2"}:
            self._heading_stack.append({"tag": tag, "text": []})

    def handle_endtag(self, tag):
        if tag == "a" and self._link_stack:
            link = self._link_stack.pop()
            text = _clean_text(" ".join(link["text"]))

            if text:
                self.nodes.append(
                    {
                        "type": "link",
                        "href": link["href"],
                        "text": text,
                    }
                )

            self._current_link = self._link_stack[-1] if self._link_stack else None

        if tag in {"h1", "h2"} and self._heading_stack:
            heading = self._heading_stack.pop()
            text = _clean_text(" ".join(heading["text"]))

            if text:
                prefix = "# " if heading["tag"] == "h1" else "## "
                self.nodes.append({"type": "text", "text": f"{prefix}{text}"})

    def handle_data(self, data):
        text = _clean_text(data)

        if not text:
            return

        if self._current_link is not None:
            self._current_link["text"].append(text)
            return

        if self._heading_stack:
            self._heading_stack[-1]["text"].append(text)
            return

        self.nodes.append({"type": "text", "text": text})
