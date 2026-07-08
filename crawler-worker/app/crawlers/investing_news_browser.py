from __future__ import annotations

import os
from pathlib import Path
from threading import Lock

from app.crawlers.investing_news_html import (
    INVESTING_CATEGORY_URLS,
    _is_cloudflare_challenge,
    parse_investing_news_html,
)


REQUEST_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "20000"))
PROFILE_DIR = Path(os.getenv("PLAYWRIGHT_PROFILE_DIR", "/data/crawler-worker/playwright-profile"))
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

_BROWSER_LOCK = Lock()


def search_investing_news_browser(
    category: str,
    limit: int = 20,
    source_filter: str = "",
) -> list[dict]:
    urls = INVESTING_CATEGORY_URLS.get(category, INVESTING_CATEGORY_URLS["WORLD"])
    articles: list[dict] = []

    for url in urls:
        html = _fetch_rendered_html(url)

        if not html or _is_cloudflare_challenge(html):
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


def _fetch_rendered_html(url: str) -> str:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ""

    with _BROWSER_LOCK:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=True,
                viewport={"width": 1440, "height": 2200},
                locale="ko-KR",
                user_agent=USER_AGENT,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )

            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT_MS)

                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except PlaywrightTimeoutError:
                    pass

                html = page.content()

                if _is_cloudflare_challenge(html):
                    try:
                        page.wait_for_timeout(4000)
                        page.reload(wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT_MS)
                        html = page.content()
                    except PlaywrightTimeoutError:
                        pass

                return html
            finally:
                context.close()


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
