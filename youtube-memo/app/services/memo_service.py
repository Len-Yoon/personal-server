import os
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


PROJECT_DATA_ROOT = Path(__file__).resolve().parents[3] / "data"
DEFAULT_DB_PATH = PROJECT_DATA_ROOT / "youtube-memo" / "youtube_memo.sqlite3"
DB_PATH = Path(os.getenv("YOUTUBE_MEMO_DB_PATH", DEFAULT_DB_PATH))


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                youtube_id TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(video_id) REFERENCES videos(id) ON DELETE CASCADE
            )
            """
        )


def create_or_get_video(url: str) -> dict[str, Any]:
    init_db()

    youtube_id = extract_youtube_id(url)

    if not youtube_id:
        raise ValueError("유효한 YouTube 링크를 입력해주세요.")

    normalized_url = f"https://www.youtube.com/watch?v={youtube_id}"
    title = f"YouTube 영상 {youtube_id}"

    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO videos (youtube_id, url, title)
            VALUES (?, ?, ?)
            ON CONFLICT(youtube_id) DO UPDATE SET
                url = excluded.url,
                updated_at = CURRENT_TIMESTAMP
            """,
            (youtube_id, normalized_url, title),
        )
        row = connection.execute(
            "SELECT * FROM videos WHERE youtube_id = ?",
            (youtube_id,),
        ).fetchone()

    return _row_to_dict(row)


def list_videos() -> list[dict[str, Any]]:
    init_db()

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT
                videos.*,
                COUNT(memos.id) AS memo_count
            FROM videos
            LEFT JOIN memos ON memos.video_id = videos.id
            GROUP BY videos.id
            ORDER BY videos.updated_at DESC, videos.id DESC
            """
        ).fetchall()

    return [_row_to_dict(row) for row in rows]


def get_video(video_id: int) -> dict[str, Any] | None:
    init_db()

    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM videos WHERE id = ?",
            (video_id,),
        ).fetchone()

    if not row:
        return None

    return _row_to_dict(row)


def delete_video(video_id: int) -> bool:
    init_db()

    with _connect() as connection:
        row = connection.execute(
            "SELECT id FROM videos WHERE id = ?",
            (video_id,),
        ).fetchone()

        if not row:
            return False

        connection.execute(
            "DELETE FROM videos WHERE id = ?",
            (video_id,),
        )

    return True


def create_memo(video_id: int, title: str, content: str) -> dict[str, Any]:
    init_db()

    title = title.strip() or "제목 없는 메모"
    content = content.strip()

    if not content:
        raise ValueError("메모 내용을 입력해주세요.")

    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO memos (video_id, title, content)
            VALUES (?, ?, ?)
            """,
            (video_id, title, content),
        )
        connection.execute(
            """
            UPDATE videos
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (video_id,),
        )
        row = connection.execute(
            """
            SELECT *
            FROM memos
            WHERE id = last_insert_rowid()
            """
        ).fetchone()

    return _row_to_dict(row)


def list_memos(video_id: int) -> list[dict[str, Any]]:
    init_db()

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM memos
            WHERE video_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (video_id,),
        ).fetchall()

    return [_row_to_dict(row) for row in rows]


def search_videos_and_memos(query: str, limit: int = 5) -> list[dict[str, Any]]:
    init_db()
    query = query.strip()
    if not query:
        return []

    keyword = f"%{query}%"
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT
                videos.id AS video_id,
                videos.title AS video_title,
                videos.url AS video_url,
                memos.title AS memo_title,
                memos.content AS memo_content
            FROM videos
            LEFT JOIN memos ON memos.video_id = videos.id
            WHERE videos.title LIKE ?
               OR videos.youtube_id LIKE ?
               OR memos.title LIKE ?
               OR memos.content LIKE ?
            ORDER BY videos.updated_at DESC, memos.created_at DESC
            LIMIT ?
            """,
            (keyword, keyword, keyword, keyword, limit),
        ).fetchall()

    return [
        {
            "title": row["memo_title"] or row["video_title"],
            "description": row["memo_content"] or row["video_url"],
            "url": f"/videos/{row['video_id']}",
        }
        for row in rows
    ]


def delete_memo(memo_id: int) -> int | None:
    init_db()

    with _connect() as connection:
        row = connection.execute(
            "SELECT video_id FROM memos WHERE id = ?",
            (memo_id,),
        ).fetchone()

        if not row:
            return None

        video_id = row["video_id"]
        connection.execute(
            "DELETE FROM memos WHERE id = ?",
            (memo_id,),
        )
        connection.execute(
            """
            UPDATE videos
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (video_id,),
        )

    return video_id


def extract_youtube_id(url: str) -> str:
    value = url.strip()

    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value

    parsed = urlparse(value)
    host = parsed.netloc.lower().replace("www.", "")

    if host == "youtu.be":
        return parsed.path.strip("/").split("/")[0]

    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        query_id = parse_qs(parsed.query).get("v", [""])[0]

        if query_id:
            return query_id

        parts = [part for part in parsed.path.split("/") if part]

        if len(parts) >= 2 and parts[0] in {"embed", "shorts", "live"}:
            return parts[1]

    return ""


def embed_url(youtube_id: str) -> str:
    return f"https://www.youtube.com/embed/{youtube_id}"


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)
