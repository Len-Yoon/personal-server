from __future__ import annotations

import re
import os
from pathlib import Path
from threading import Lock

from app.crawlers.investing_news_html import (
    INVESTING_CATEGORY_URLS,
    _is_cloudflare_challenge,
    _clean_text,
    _looks_like_summary,
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
        html = ""
        rendered_articles: list[dict] = []

        for attempt in range(2):
            page_data = _fetch_rendered_page_data(url)
            html = page_data["html"]

            if not html or _is_cloudflare_challenge(html):
                continue

            rendered_articles = _parse_rendered_articles(
                body_text=page_data["body_text"],
                cards=page_data["cards"],
                category=category,
                page_url=url,
            )

            if rendered_articles:
                break

        if rendered_articles:
            articles.extend(rendered_articles)
        elif html:
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


def _fetch_rendered_page_data(url: str) -> dict:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"html": "", "body_text": "", "cards": []}

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
                body_text = page.locator("body").inner_text()
                cards = _extract_article_cards(page)

                if _is_cloudflare_challenge(html):
                    try:
                        page.wait_for_timeout(4000)
                        page.reload(wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT_MS)
                        html = page.content()
                        body_text = page.locator("body").inner_text()
                        cards = _extract_article_cards(page)
                    except PlaywrightTimeoutError:
                        pass

                return {
                    "html": html,
                    "body_text": body_text,
                    "cards": cards,
                }
            finally:
                context.close()


def _extract_article_cards(page) -> list[dict]:
    cards = page.locator('article a[href*="/article-"]').evaluate_all(
        """
        (elements) => elements.map((element) => {
            const titleElement = element.querySelector('p[title]') || element.querySelector('p');
            const title = (titleElement?.getAttribute('title') || titleElement?.innerText || element.innerText || '').trim();
            return {
                title,
                href: element.href || '',
            };
        })
        """
    )

    return [
        card
        for card in cards
        if card.get("href") and "/article-" in card["href"] and card.get("title")
    ]


def _parse_rendered_articles(
    body_text: str,
    cards: list[dict],
    category: str,
    page_url: str,
) -> list[dict]:
    lines = _clean_lines(body_text)
    if not lines or not cards:
        return []

    card_index = 0
    index = 0
    articles: list[dict] = []

    while index < len(lines) and card_index < len(cards):
        line = lines[index]
        card = cards[card_index]
        title = card.get("title", "")

        if line != title:
            index += 1
            continue

        details, next_index = _parse_rendered_article_block(lines, index + 1)
        source = details["source"] or "Investing.com"

        articles.append(
            {
                "category": category,
                "title": title,
                "title_ko": title,
                "title_original": title,
                "url": card.get("href", page_url),
                "source": source,
                "published_at": details["published_at"],
                "published_at_sort": "",
                "summary": details["summary"],
                "provider": "Investing.com KR",
            }
        )

        card_index += 1
        index = next_index

    return articles


def _parse_rendered_article_block(lines: list[str], index: int) -> tuple[dict, int]:
    summary = ""
    source = ""
    published_at = ""

    if index < len(lines):
        first = lines[index]

        if _looks_like_summary(first):
            summary = first
            index += 1

    if index < len(lines):
        inline_byline = re.match(r"^By\s*(?P<source>[^•]+?)\s*•\s*(?P<published>.+)$", lines[index])
        if inline_byline:
            source = _clean_text(inline_byline.group("source"))
            published_at = _clean_text(inline_byline.group("published"))
            return {"summary": summary, "source": source, "published_at": published_at}, index + 1

    if index < len(lines) and lines[index] == "By":
        index += 1
        if index < len(lines):
            source = lines[index]
            index += 1
        if index < len(lines) and lines[index] == "•":
            index += 1
        if index < len(lines):
            published_at = lines[index]
            index += 1

    return {"summary": summary, "source": source, "published_at": published_at}, index


def _clean_lines(body_text: str) -> list[str]:
    lines = []

    for raw_line in body_text.splitlines():
        line = _clean_text(raw_line)

        if not line:
            continue

        if line in _BODY_SKIP_TEXTS:
            continue

        if line.startswith("WarrenAI") or line.startswith("🤖 "):
            continue

        lines.append(line)

    return lines


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


_BODY_SKIP_TEXTS = {
    "60% 할인: 줄라이 세일 🏝️",
    "로그인",
    "무료 회원가입",
    "시장",
    "내 관심목록",
    "투자 챌린지",
    "차트",
    "뉴스",
    "분석",
    "기술적 분석",
    "브로커",
    "도구 모음",
    "교육",
    "알림",
    "경제 캘린더",
    "주식 스크리너",
    "더보기",
    "최신",
    "많이 본",
    "뉴스 속보",
    "경제",
    "주식 시장",
    "경제 지표",
    "상품과 선물",
    "외환",
    "암호화폐",
    "IPO",
    "일반",
    "내부자 거래",
    "애널리스트 투자의견",
    "실적",
    "스크립트",
    "광고",
    "할인받기",
    "WarrenAI에게 질문하기",
}
