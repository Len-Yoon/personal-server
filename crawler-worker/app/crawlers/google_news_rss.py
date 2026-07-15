from __future__ import annotations

from app.crawlers.rss_news import build_google_news_rss_url, search_rss_news


GOOGLE_NEWS_QUERIES = {
    "WORLD": "world markets OR global markets OR inflation OR rates",
    "NASDAQ": "nasdaq OR semiconductors OR artificial intelligence OR tech stocks",
    "GOLD": "gold prices OR gold futures OR dollar OR inflation",
    "HK50": "hang seng OR hong kong stocks OR china markets",
    "KR_WORLD": "세계 뉴스 OR 글로벌 경제 OR 금리 OR 환율 OR 인플레이션",
    "KR_IT": "IT 동향 OR 클라우드 OR 개발자 도구 OR 플랫폼 엔지니어링 OR 소프트웨어",
    "KR_AI": "AI 뉴스 OR 인공지능 OR LLM OR 생성형 AI OR AI 에이전트",
    "KR_STACK": "React OR Next.js OR FastAPI OR Spring Boot OR TypeScript OR Kubernetes",
}


def search_google_news_rss(
    category: str,
    limit: int = 20,
    source_filter: str = "",
) -> list[dict]:
    category = category.upper()
    query = GOOGLE_NEWS_QUERIES.get(category, GOOGLE_NEWS_QUERIES["WORLD"])
    feed_url = build_google_news_rss_url(query, freshness="1d")

    return search_rss_news(
        feed_urls=[feed_url],
        category=category,
        source_name="Google News",
        provider_name="Google News RSS",
        limit=limit,
        source_filter=source_filter,
        today_only=True,
    )[:limit]


def filter_korean_articles(articles: list[dict]) -> list[dict]:
    return [
        article
        for article in articles
        if _has_korean_text(
            " ".join(
                str(article.get(field, ""))
                for field in ("title", "title_ko", "summary", "source")
            )
        )
    ]


def _has_korean_text(value: str) -> bool:
    return any("\uac00" <= char <= "\ud7a3" for char in value)
