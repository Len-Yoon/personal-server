import time

from app.crawlers.google_news_rss import search_google_news_rss


CACHE_TTL_SECONDS = 300
RSS_FETCH_LIMIT = 80
RSS_FRESHNESS = "1d"

_news_cache = {}


CATEGORY_CONFIG = {
    "WORLD": {
        "label": "세계 뉴스",
        "description": "전쟁, 금리, 달러, 원자재, 주요국 경제 이슈",
        "query": "세계 경제 OR 미국 금리 OR 연준 OR 인플레이션 OR 달러 OR 환율 OR 원자재 OR 전쟁",
    },
    "NASDAQ": {
        "label": "나스닥 선물",
        "description": "NASDAQ, 미국 기술주, 반도체, AI, 미국 금리 이슈",
        "query": "나스닥 선물 OR 나스닥100 OR 미국 증시 OR 기술주 OR 엔비디아 OR AI 반도체 OR 연준 금리",
    },
    "GOLD": {
        "label": "금 선물",
        "description": "Gold futures, 달러, 금리, 안전자산, 인플레이션",
        "query": "금 선물 OR 금값 OR 국제 금값 OR 달러 OR 미국 국채금리 OR 안전자산 OR 인플레이션",
    },
    "HK50": {
        "label": "홍콩50",
        "description": "Hang Seng, Hong Kong 50, 중국 증시, 중국 경기 이슈",
        "query": "항셍지수 OR 홍콩 증시 OR 홍콩H지수 OR 중국 증시 OR 중국 경기 OR 홍콩50",
    },
}


def get_categories():
    return [
        {
            "code": code,
            "label": config["label"],
            "description": config["description"],
        }
        for code, config in CATEGORY_CONFIG.items()
    ]


def collect_market_news(
    category: str,
    limit: int = 24,
    force_refresh: bool = False,
):
    category = category.upper()

    if category not in CATEGORY_CONFIG:
        category = "WORLD"

    cache_key = f"{category}:{limit}"
    now = time.time()

    cached = _news_cache.get(cache_key)

    if cached and not force_refresh:
        age = now - cached["created_at"]

        if age < CACHE_TTL_SECONDS:
            result = cached["data"].copy()
            result["cache"] = {
                "hit": True,
                "age_seconds": int(age),
                "ttl_seconds": CACHE_TTL_SECONDS,
            }
            return result

    config = CATEGORY_CONFIG[category]
    query = config["query"]

    articles = search_google_news_rss(
        query=query,
        category=category,
        limit=RSS_FETCH_LIMIT,
        freshness=RSS_FRESHNESS,
    )

    merged = _merge_articles(articles)

    result = {
        "category": category,
        "label": config["label"],
        "description": config["description"],
        "query": query,
        "count": len(merged[:limit]),
        "articles": merged[:limit],
        "cache": {
            "hit": False,
            "age_seconds": 0,
            "ttl_seconds": CACHE_TTL_SECONDS,
        },
    }

    _news_cache[cache_key] = {
        "created_at": now,
        "data": result,
    }

    return result


def _merge_articles(articles):
    seen = set()
    merged = []

    for article in articles:
        url = article.get("url")

        if not url:
            continue

        if url in seen:
            continue

        seen.add(url)
        merged.append(article)

    return merged
