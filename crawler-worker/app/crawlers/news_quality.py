from __future__ import annotations

import re


_JUNK_TEXTS = {
    "광고",
    "더보기",
    "최신",
    "많이 본",
    "뉴스 속보",
    "warrenai",
    "ai",
}


def filter_high_quality_articles(
    articles: list[dict],
    category: str = "",
    limit: int = 20,
) -> list[dict]:
    scored_articles: list[tuple[int, int, dict]] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()

    for index, article in enumerate(articles):
        url = _normalize_text(article.get("url", ""))
        title = _normalize_text(article.get("title", ""))
        summary = _normalize_text(article.get("summary", ""))
        source = _normalize_text(article.get("source", ""))

        if not url or not title:
            continue

        title_key = title.casefold()
        if url in seen_urls or title_key in seen_titles:
            continue

        seen_urls.add(url)
        seen_titles.add(title_key)

        score = _score_article(title=title, summary=summary, source=source, category=category)

        if score < 3:
            continue

        scored_articles.append((score, index, article))

    scored_articles.sort(key=lambda item: (-item[0], item[1]))
    return [article for _, _, article in scored_articles[:limit]]


def _score_article(title: str, summary: str, source: str, category: str) -> int:
    score = 0

    if _contains_junk(title) or _contains_junk(summary):
        return -10

    title_length = len(title)
    summary_length = len(summary)

    if title_length >= 18:
        score += 2
    elif title_length >= 12:
        score += 1

    if summary_length >= 50:
        score += 2
    elif summary_length >= 20:
        score += 1

    if summary and summary.casefold() != title.casefold():
        score += 1
    else:
        score -= 1

    if source:
        score += 1

    if source.casefold() in {"ap news", "marketwatch", "reuters", "ft"}:
        score += 1

    if category and _matches_category(title, summary, category):
        score += 1

    if len(title) < 10:
        score -= 2

    if len(summary) < 10:
        score -= 1

    return score


def _matches_category(title: str, summary: str, category: str) -> bool:
    haystack = f"{title} {summary}".casefold()
    category = category.upper()

    keyword_sets = {
        "WORLD": ("미국", "유럽", "중국", "전쟁", "연준", "달러", "금리", "인플레이션"),
        "NASDAQ": ("나스닥", "반도체", "엔비디아", "테크", "AI", "미국 증시", "연준"),
        "GOLD": ("금", "달러", "금리", "인플레이션", "안전자산", "국채"),
        "HK50": ("홍콩", "항셍", "중국", "부동산", "위안", "증시"),
    }

    for keyword in keyword_sets.get(category, ()):
        if keyword.casefold() in haystack:
            return True

    return False


def _contains_junk(text: str) -> bool:
    lowered = text.casefold()
    return any(junk in lowered for junk in _JUNK_TEXTS)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()
