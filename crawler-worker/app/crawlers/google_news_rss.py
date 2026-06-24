from urllib.parse import quote_plus

import feedparser


def search_google_news_rss(query: str, category: str, limit: int = 20):
    encoded_query = quote_plus(query)

    rss_url = (
        "https://news.google.com/rss/search?"
        f"q={encoded_query}"
        "&hl=en-US"
        "&gl=US"
        "&ceid=US:en"
    )

    feed = feedparser.parse(rss_url)

    articles = []

    for entry in feed.entries[:limit]:
        title = getattr(entry, "title", "")
        link = getattr(entry, "link", "")
        published = getattr(entry, "published", "")
        source = "Google News RSS"

        if hasattr(entry, "source") and hasattr(entry.source, "title"):
            source = entry.source.title

        if not title or not link:
            continue

        articles.append(
            {
                "category": category,
                "title": title,
                "url": link,
                "source": source,
                "published_at": published,
                "summary": "",
                "provider": "Google News RSS",
            }
        )

    return articles