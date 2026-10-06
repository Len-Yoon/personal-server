import os
import re
import sqlite3
import json
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import urlopen

from app.services.datetime_format import format_display_datetime
from app.services.write_safety import WriteConflict, check_version, init_write_requests, record_result, replay_result


PROJECT_DATA_ROOT = Path(__file__).resolve().parents[3] / "data"
DEFAULT_DB_PATH = PROJECT_DATA_ROOT / "youtube-memo" / "youtube_memo.sqlite3"
DB_PATH = Path(os.getenv("YOUTUBE_MEMO_DB_PATH", DEFAULT_DB_PATH))
MEMO_TIMESTAMP_PATTERN = re.compile(r"(?<![\w:])(?:\d{1,2}:\d{2}:\d{2}|\d{1,4}:\d{2})(?![\w:])")


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
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
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_videos_home_order ON videos (updated_at DESC, id DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_memos_video ON memos (video_id)"
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS memo_tags (
                memo_id INTEGER NOT NULL,
                tag TEXT NOT NULL,
                tag_key TEXT NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY (memo_id, tag_key),
                FOREIGN KEY (memo_id) REFERENCES memos(id) ON DELETE CASCADE
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_memo_tags_key ON memo_tags (tag_key, memo_id)")
        if "version" not in {row["name"] for row in connection.execute("PRAGMA table_info(memos)")}:
            connection.execute("ALTER TABLE memos ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        init_write_requests(connection)


def parse_tags(value: str) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for part in value.split(","):
        tag = part.strip()
        if not tag:
            continue
        if len(tag) > 30 or any(unicodedata.category(char).startswith("C") for char in tag):
            raise ValueError("태그는 30자 이내이며 제어 문자를 포함할 수 없습니다.")
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            tags.append(tag)
        if len(tags) > 5:
            raise ValueError("태그는 최대 5개까지 입력할 수 있습니다.")
    return tags


def _replace_tags(connection: sqlite3.Connection, memo_id: int, tags: list[str]) -> None:
    connection.execute("DELETE FROM memo_tags WHERE memo_id = ?", (memo_id,))
    connection.executemany(
        "INSERT INTO memo_tags (memo_id, tag, tag_key, position) VALUES (?, ?, ?, ?)",
        [(memo_id, tag, tag.casefold(), position) for position, tag in enumerate(tags)],
    )


def create_or_get_video(
    url: str,
    title_fetcher: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    init_db()

    youtube_id = extract_youtube_id(url)

    if not youtube_id:
        raise ValueError("유효한 YouTube 링크를 입력해주세요.")

    normalized_url = f"https://www.youtube.com/watch?v={youtube_id}"
    title_fetcher = title_fetcher or fetch_youtube_title
    title = title_fetcher(youtube_id, normalized_url).strip() or f"YouTube 영상 {youtube_id}"

    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO videos (youtube_id, url, title)
            VALUES (?, ?, ?)
            ON CONFLICT(youtube_id) DO UPDATE SET
                url = excluded.url,
                title = CASE
                    WHEN excluded.title NOT LIKE 'YouTube 영상 %' THEN excluded.title
                    ELSE videos.title
                END,
                updated_at = CURRENT_TIMESTAMP
            """,
            (youtube_id, normalized_url, title),
        )
        row = connection.execute(
            "SELECT * FROM videos WHERE youtube_id = ?",
            (youtube_id,),
        ).fetchone()

    return _row_to_dict(row)


def fetch_youtube_title(youtube_id: str, url: str) -> str:
    oembed_url = f"https://www.youtube.com/oembed?url={quote(url, safe='')}&format=json"
    try:
        with urlopen(oembed_url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return ""
    return str(payload.get("title", "")).strip()


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


def list_videos_page(page: int, page_size: int = 24, tag: str = "") -> tuple[list[dict[str, Any]], int, int]:
    init_db()
    tag_key = tag.strip().casefold()
    where = "WHERE EXISTS (SELECT 1 FROM memos JOIN memo_tags ON memo_tags.memo_id = memos.id WHERE memos.video_id = videos.id AND memo_tags.tag_key = ?)" if tag_key else ""
    parameters: tuple[Any, ...] = (tag_key,) if tag_key else ()
    with _connect() as connection:
        connection.execute("BEGIN")
        count_query = f"SELECT COUNT(*) FROM videos {where}" if tag_key else "SELECT COUNT(*) FROM videos"
        total = connection.execute(count_query, parameters).fetchone()[0]
        last_page = max(1, (total + page_size - 1) // page_size)
        page = min(max(1, page), last_page)
        rows = connection.execute(
            """
            WITH selected AS (
                SELECT * FROM videos
                {where}
                ORDER BY updated_at DESC, id DESC
                LIMIT ? OFFSET ?
            )
            SELECT selected.*,
                (SELECT COUNT(*) FROM memos WHERE video_id = selected.id) AS memo_count
            FROM selected
            ORDER BY selected.updated_at DESC, selected.id DESC
            """.replace("{where}", where),
            (*parameters, page_size, (page - 1) * page_size),
        ).fetchall()

    return [_row_to_dict(row) for row in rows], total, page


def list_available_tags() -> list[dict[str, Any]]:
    init_db()
    with _connect() as connection:
        rows = connection.execute(
            """SELECT tag_key, MIN(tag) AS tag, COUNT(DISTINCT memos.video_id) AS video_count
               FROM memo_tags JOIN memos ON memos.id = memo_tags.memo_id
               GROUP BY tag_key ORDER BY tag_key"""
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


def create_memo(video_id: int, title: str, content: str, tags: str = "", request_id: str = "") -> dict[str, Any]:
    init_db()

    title = title.strip() or "제목 없는 메모"
    content = content.strip()

    if not content:
        raise ValueError("메모 내용을 입력해주세요.")
    parsed_tags = parse_tags(tags)

    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        payload = [video_id, title, content, parsed_tags]
        replayed = replay_result(connection, request_id, "memo-create", payload)
        if replayed is not None:
            row = connection.execute("SELECT * FROM memos WHERE id = ?", (replayed,)).fetchone()
            if not row:
                raise WriteConflict("이미 저장 후 삭제된 요청입니다. 새 메모는 새 요청으로 저장해주세요.")
            return _row_to_dict(row)
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
        _replace_tags(connection, row["id"], parsed_tags)
        record_result(connection, request_id, "memo-create", payload, row["id"])

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
        tag_rows = connection.execute(
            """SELECT memo_tags.memo_id, memo_tags.tag FROM memo_tags
               JOIN memos ON memos.id = memo_tags.memo_id
               WHERE memos.video_id = ? ORDER BY memo_tags.memo_id, memo_tags.position""",
            (video_id,),
        ).fetchall()

    tags_by_memo: dict[int, list[str]] = {}
    for tag_row in tag_rows:
        tags_by_memo.setdefault(tag_row["memo_id"], []).append(tag_row["tag"])

    return [
        {
            **_row_to_dict(row),
            "display_created_at": format_display_datetime(row["created_at"]),
            "timestamp_segments": memo_timestamp_segments(row["content"]),
            "tags": tags_by_memo.get(row["id"], []),
        }
        for row in rows
    ]


def list_export_records(video_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """Return saved videos and their memos from one consistent database snapshot."""
    init_db()
    with _connect() as connection:
        connection.execute("BEGIN")
        where = f" WHERE id IN ({','.join('?' for _ in video_ids)})" if video_ids is not None else ""
        videos = connection.execute(
            "SELECT id, youtube_id, url, title, created_at, updated_at FROM videos" + where + " ORDER BY id",
            video_ids or [],
        ).fetchall()
        if video_ids is not None and len(videos) != len(video_ids):
            raise ValueError("선택한 영상 중 찾을 수 없는 항목이 있습니다.")
        memo_where = f" WHERE video_id IN ({','.join('?' for _ in video_ids)})" if video_ids is not None else ""
        memos = connection.execute(
            "SELECT id, video_id, title, content, created_at, updated_at FROM memos" + memo_where + " ORDER BY video_id, id",
            video_ids or [],
        ).fetchall()
        if video_ids is None:
            tags = connection.execute("SELECT memo_id, tag FROM memo_tags ORDER BY memo_id, position").fetchall()
        else:
            tags = connection.execute(
                "SELECT memo_tags.memo_id, memo_tags.tag FROM memo_tags "
                "JOIN memos ON memos.id = memo_tags.memo_id" + memo_where.replace("video_id", "memos.video_id") +
                " ORDER BY memo_tags.memo_id, memo_tags.position",
                video_ids,
            ).fetchall()

    tags_by_memo: dict[int, list[str]] = {}
    for row in tags:
        tags_by_memo.setdefault(row["memo_id"], []).append(row["tag"])

    records = [{**_row_to_dict(row), "memos": []} for row in videos]
    records_by_id = {record["id"]: record for record in records}
    for row in memos:
        memo = _row_to_dict(row)
        memo["tags"] = tags_by_memo.get(memo["id"], [])
        memo["timestamps"] = [
            segment for segment in memo_timestamp_segments(memo["content"])
            if segment["seconds"] is not None
        ]
        records_by_id[memo["video_id"]]["memos"].append(memo)
    return records


def update_memo_tags(memo_id: int, tags: str, request_id: str = "") -> int | None:
    init_db()
    parsed_tags = parse_tags(tags)
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        payload = [memo_id, parsed_tags]
        replayed = replay_result(connection, request_id, "memo-tags", payload)
        if replayed is not None:
            return replayed
        row = connection.execute("SELECT video_id FROM memos WHERE id = ?", (memo_id,)).fetchone()
        if row is None:
            return None
        _replace_tags(connection, memo_id, parsed_tags)
        connection.execute("UPDATE memos SET version = version + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (memo_id,))
        connection.execute("UPDATE videos SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (row["video_id"],))
        record_result(connection, request_id, "memo-tags", payload, row["video_id"])
    return row["video_id"]


def memo_timestamp_segments(content: str) -> list[dict[str, Any]]:
    """Split display text into escaped-by-template text and valid video positions."""
    segments: list[dict[str, Any]] = []
    cursor = 0
    for match in MEMO_TIMESTAMP_PATTERN.finditer(content):
        fields = [int(field) for field in match.group().split(":")]
        if len(fields) == 3:
            hours, minutes, seconds = fields
            if minutes >= 60:
                continue
        else:
            hours = 0
            minutes, seconds = fields
        position = hours * 3600 + minutes * 60 + seconds
        if seconds >= 60 or position >= 24 * 3600:
            continue
        if match.start() > cursor:
            segments.append({"text": content[cursor:match.start()], "seconds": None})
        segments.append({"text": match.group(), "seconds": position})
        cursor = match.end()
    if cursor < len(content) or not segments:
        segments.append({"text": content[cursor:], "seconds": None})
    return segments


def search_videos_and_memos(query: str, limit: int = 5) -> list[dict[str, Any]]:
    init_db()
    query = query.strip()
    if not query:
        return []

    escaped_query = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    keyword = f"%{escaped_query}%"
    with _connect() as connection:
        rows = connection.execute(
            """
            WITH matches AS (
                SELECT videos.id AS video_id, videos.title AS video_title,
                    videos.url AS video_url, videos.updated_at AS video_updated,
                    memos.id AS memo_id, memos.title AS memo_title,
                    memos.content AS memo_content, memos.updated_at AS memo_updated,
                    CASE
                        WHEN videos.title = ? COLLATE NOCASE THEN 0
                        WHEN memos.title = ? COLLATE NOCASE THEN 1
                        WHEN videos.title LIKE ? ESCAPE '!' THEN 2
                        WHEN memos.title LIKE ? ESCAPE '!' THEN 3
                        WHEN videos.youtube_id LIKE ? ESCAPE '!' THEN 4
                        ELSE 5
                    END AS rank
                FROM videos LEFT JOIN memos ON memos.video_id = videos.id
                    AND (memos.title LIKE ? ESCAPE '!' OR memos.content LIKE ? ESCAPE '!')
                WHERE videos.title LIKE ? ESCAPE '!'
                   OR videos.youtube_id LIKE ? ESCAPE '!'
                   OR memos.title LIKE ? ESCAPE '!'
                   OR memos.content LIKE ? ESCAPE '!'
            ), ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY video_id ORDER BY rank, memo_updated DESC, memo_id DESC
                ) AS video_rank FROM matches
            )
            SELECT * FROM ranked WHERE video_rank = 1
            ORDER BY rank, video_updated DESC, video_id DESC LIMIT ?
            """,
            (query, query, keyword, keyword, keyword, keyword, keyword, keyword, keyword, keyword, keyword, limit),
        ).fetchall()

    return [
        {
            "title": row["video_title"] if row["rank"] in {0, 2, 4} else row["memo_title"] or row["video_title"],
            "description": row["video_title"] or row["video_url"],
            "snippet": _snippet(row["memo_content"] or "", query),
            "meta": "YouTube 메모",
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


def update_memo(memo_id: int, title: str, content: str, expected_version: int | None = None, request_id: str = "") -> int | None:
    init_db()
    title = title.strip() or "제목 없는 메모"
    content = content.strip()

    if not content:
        raise ValueError("메모 내용을 입력해주세요.")

    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        payload = [memo_id, title, content, expected_version]
        replayed = replay_result(connection, request_id, "memo-edit", payload)
        if replayed is not None:
            return replayed
        row = connection.execute(
            "SELECT video_id, version FROM memos WHERE id = ?",
            (memo_id,),
        ).fetchone()

        if not row:
            return None

        check_version(row, expected_version)
        video_id = row["video_id"]
        connection.execute(
            """
            UPDATE memos
            SET title = ?, content = ?, version = version + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (title, content, memo_id),
        )
        connection.execute(
            """
            UPDATE videos
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (video_id,),
        )
        record_result(connection, request_id, "memo-edit", payload, video_id)

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


def _snippet(value: str, query: str = "", limit: int = 140) -> str:
    cleaned = " ".join(value.strip().split())
    if len(cleaned) <= limit:
        return cleaned
    position = cleaned.casefold().find(query.casefold()) if query else -1
    start = max(0, position - 45) if position >= 0 else 0
    start = min(start, len(cleaned) - limit)
    excerpt = cleaned[start:start + limit].strip()
    return f"{'...' if start else ''}{excerpt}{'...' if start + limit < len(cleaned) else ''}"


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)
