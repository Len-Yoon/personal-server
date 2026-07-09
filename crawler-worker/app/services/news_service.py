from __future__ import annotations

from app.services.news_archive import (
    collect_market_news as _collect_market_news,
    get_categories,
)


def collect_market_news(
    category: str,
    limit: int = 24,
    force_refresh: bool = False,
):
    return _collect_market_news(
        category=category,
        limit=limit,
        force_refresh=force_refresh,
    )
