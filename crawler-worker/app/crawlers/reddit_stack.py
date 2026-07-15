from __future__ import annotations

from app.crawlers.rss_news import search_rss_news


REDDIT_STACK_FEEDS = (
    ("programming", "프로그래밍"),
    ("webdev", "웹 개발"),
    ("reactjs", "React"),
    ("nextjs", "Next.js"),
    ("FastAPI", "FastAPI"),
    ("kubernetes", "Kubernetes"),
    ("typescript", "TypeScript"),
)


def search_reddit_stack_posts(limit: int = 20) -> list[dict]:
    feed_urls = [
        f"https://www.reddit.com/r/{subreddit}/hot/.rss?limit=10"
        for subreddit, _ in REDDIT_STACK_FEEDS
    ]
    articles = search_rss_news(
        feed_urls=feed_urls,
        category="KR_STACK",
        source_name="Reddit",
        provider_name="Reddit Hot RSS",
        limit=limit,
        today_only=False,
    )

    subreddit_topics = {
        f"/r/{subreddit}/": topic for subreddit, topic in REDDIT_STACK_FEEDS
    }
    for article in articles:
        topic = next(
            (
                topic
                for marker, topic in subreddit_topics.items()
                if marker in str(article.get("url", ""))
            ),
            "개발 트렌드",
        )
        article["topics"] = [topic]
        article["source_status"] = "reddit"

    return articles[:limit]
