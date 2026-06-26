import json
import os
import sqlite3
from pathlib import Path
from typing import Any


PROJECT_DATA_ROOT = Path(__file__).resolve().parents[3] / "data"
DEFAULT_DB_PATH = PROJECT_DATA_ROOT / "crawler-worker" / "news_summaries.sqlite3"
DB_PATH = Path(os.getenv("NEWS_DB_PATH", DEFAULT_DB_PATH))


def init_saved_news_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                title_ko TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                summary_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_saved_news_search
            ON saved_news(title_ko, title, source, category)
            """
        )


def save_news_summary(result: dict[str, Any]) -> dict[str, Any]:
    init_saved_news_db()

    article = result.get("article", {})
    summary = result.get("summary", {})
    title = article.get("title_original") or article.get("title", "")
    title_ko = summary.get("title_ko") or article.get("title_ko") or article.get("title", "")
    url = article.get("url", "")

    if not url:
        return {
            "saved": False,
            "error": "url is required",
        }

    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO saved_news (
                category,
                title,
                title_ko,
                url,
                source,
                provider,
                published_at,
                model,
                summary_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                category = excluded.category,
                title = excluded.title,
                title_ko = excluded.title_ko,
                source = excluded.source,
                provider = excluded.provider,
                published_at = excluded.published_at,
                model = excluded.model,
                summary_json = excluded.summary_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                article.get("category", ""),
                title,
                title_ko,
                url,
                article.get("source", ""),
                article.get("provider", ""),
                article.get("published_at", ""),
                result.get("model", ""),
                json.dumps(summary, ensure_ascii=False),
            ),
        )

        row = connection.execute(
            "SELECT id, created_at, updated_at FROM saved_news WHERE url = ?",
            (url,),
        ).fetchone()

    return {
        "saved": True,
        "id": row["id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_saved_news_by_url(url: str) -> dict[str, Any] | None:
    init_saved_news_db()

    if not url:
        return None

    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM saved_news WHERE url = ?",
            (url,),
        ).fetchone()

    if not row:
        return None

    return _row_to_saved_news(row)


def delete_saved_news(saved_news_id: int) -> bool:
    init_saved_news_db()

    with _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM saved_news WHERE id = ?",
            (saved_news_id,),
        )

    return cursor.rowcount > 0


def search_saved_news(query: str = "", limit: int = 50) -> list[dict[str, Any]]:
    init_saved_news_db()

    query = query.strip()
    params: list[Any] = []
    where = ""

    if query:
        keyword = f"%{query}%"
        where = """
        WHERE title_ko LIKE ?
           OR title LIKE ?
           OR source LIKE ?
           OR category LIKE ?
           OR summary_json LIKE ?
        """
        params = [keyword, keyword, keyword, keyword, keyword]

    params.append(limit)

    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT *
            FROM saved_news
            {where}
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    return [_row_to_saved_news(row) for row in rows]


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _row_to_saved_news(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)

    try:
        item["summary"] = json.loads(item.pop("summary_json"))
    except json.JSONDecodeError:
        item["summary"] = {}

    return item
