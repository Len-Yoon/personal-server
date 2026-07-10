from __future__ import annotations

import re
from datetime import datetime


URL_PATTERN = re.compile(r"https://kr\.investing\.com/news/[^)\s]+")


def render_daily_markdown(
    news: list[dict[str, str]], collected_at: datetime, source_url: str
) -> str:
    date_label = collected_at.strftime("%Y-%m-%d")
    collected_label = collected_at.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# Investing.com 한국어 뉴스 - {date_label}",
        "",
        f"수집 시각: {collected_label}",
        f"수집 대상: {source_url}",
        "",
        "## 뉴스 목록",
        "",
    ]
    lines.extend(_render_items(news))
    return "\n".join(lines).rstrip() + "\n"


def merge_daily_markdown(
    existing: str,
    news: list[dict[str, str]],
    collected_at: datetime,
    source_url: str,
) -> str:
    if not existing.strip():
        return render_daily_markdown(news, collected_at, source_url)

    existing_urls = set(URL_PATTERN.findall(existing))
    new_items = [item for item in news if item.get("url") not in existing_urls]
    if not new_items:
        return existing.rstrip() + "\n"

    base = existing.rstrip() + "\n\n"
    return base + "\n".join(_render_items(new_items)).rstrip() + "\n"


def _render_items(news: list[dict[str, str]]) -> list[str]:
    lines: list[str] = []
    for item in news:
        lines.extend(
            [
                f"- [{item.get('title', '').strip()}]({item.get('url', '').strip()})",
                f"  - 게시 표시: {item.get('published_label', '').strip() or '확인 불가'}",
                f"  - 출처: {item.get('source', '').strip() or 'Investing.com'}",
                "",
            ]
        )
    return lines
