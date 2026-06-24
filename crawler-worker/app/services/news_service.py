from app.crawlers.gdelt_client import search_gdelt_articles
from app.crawlers.google_news_rss import search_google_news_rss


CATEGORY_CONFIG = {
    "WORLD": {
        "label": "세계 뉴스",
        "description": "전쟁, 금리, 달러, 원자재, 주요국 경제 이슈",
        "query": '("global economy" OR "central bank" OR inflation OR "interest rates" OR dollar OR geopolitics OR war)',
    },
    "NASDAQ": {
        "label": "나스닥 선물",
        "description": "NASDAQ, 미국 기술주, 반도체, AI, 미국 금리 이슈",
        "query": '("Nasdaq futures" OR "Nasdaq 100" OR "US tech stocks" OR Nvidia OR "AI stocks" OR "Fed rate")',
    },
    "GOLD": {
        "label": "금 선물",
        "description": "Gold futures, 달러, 금리, 안전자산, 인플레이션",
        "query": '("gold futures" OR "gold price" OR XAUUSD OR "safe haven" OR "US dollar" OR "Treasury yields")',
    },
    "HK50": {
        "label": "홍콩50",
        "description": "Hang Seng, Hong Kong 50, 중국 증시, 중국 경기 이슈",
        "query": '("Hang Seng" OR "Hong Kong stocks" OR "Hong Kong 50" OR "China stocks" OR "HSI futures")',
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


def collect_market_news(category: str, limit: int = 20):
    category = category.upper()

    if category not in CATEGORY_CONFIG:
        category = "WORLD"

    config = CATEGORY_CONFIG[category]
    query = config["query"]

    gdelt_articles = search_gdelt_articles(
        query=query,
        category=category,
        limit=limit,
    )

    rss_articles = search_google_news_rss(
        query=query,
        category=category,
        limit=limit,
    )

    merged = _merge_articles(gdelt_articles + rss_articles)

    return {
        "category": category,
        "label": config["label"],
        "description": config["description"],
        "query": query,
        "count": len(merged[:limit]),
        "articles": merged[:limit],
    }


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