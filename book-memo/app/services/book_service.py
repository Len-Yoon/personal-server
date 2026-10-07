import os
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.services.datetime_format import format_display_datetime
from app.services.write_safety import WriteConflict, check_version, init_write_requests, record_result, replay_result


PROJECT_DATA_ROOT = next(
    (
        parent / "data"
        for parent in Path(__file__).resolve().parents
        if (parent / "docker-compose.yml").exists()
    ),
    Path("/data"),
)
DEFAULT_DB_PATH = PROJECT_DATA_ROOT / "book-memo" / "book_memo.sqlite3"
DB_PATH = Path(os.getenv("BOOK_MEMO_DB_PATH", DEFAULT_DB_PATH))


def _schema_ready(connection: sqlite3.Connection) -> bool:
    """Inspect schema without reserving the SQLite writer lock."""
    required = {'books', 'idx_memo_tags_key', 'memo_tags', 'book_memos', 'idx_books_home_order', 'idx_book_chapters_book', 'book_chapters', 'write_requests', 'idx_book_memos_book'}
    present = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master")}
    if not required <= present:
        return False
    return all(
        "version" in {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for table in ('books', 'book_chapters', 'book_memos')
    )


def init_db() -> None:
    # DB_PATH is resolved at each call so test fixtures and runtime overrides work.
    # Ordinary reads only inspect the schema; migrations alone acquire the writer.
    if DB_PATH.is_file():
        with _connect() as connection:
            if _schema_ready(connection):
                return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if _schema_ready(connection):
            return
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                isbn TEXT NOT NULL UNIQUE,
                external_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                authors TEXT NOT NULL DEFAULT '',
                publisher TEXT NOT NULL DEFAULT '',
                published_date TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                thumbnail TEXT NOT NULL DEFAULT '',
                preview_url TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                reading_status TEXT NOT NULL DEFAULT '읽는 중',
                current_page INTEGER NOT NULL DEFAULT 0,
                current_chapter TEXT NOT NULL DEFAULT '',
                progress_percent INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS book_chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                is_done INTEGER NOT NULL DEFAULT 0,
                comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS book_memos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                chapter_id INTEGER,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                page INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE,
                FOREIGN KEY(chapter_id) REFERENCES book_chapters(id) ON DELETE SET NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_books_home_order ON books (
                CASE
                    WHEN reading_status = '읽는 중' THEN 0
                    WHEN reading_status = '읽을 예정' THEN 1
                    WHEN progress_percent >= 100 OR reading_status = '완료' THEN 2
                    ELSE 3
                END,
                updated_at DESC,
                id DESC
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_book_chapters_book ON book_chapters (book_id, position, id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_book_memos_book ON book_memos (book_id)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memo_tags (
                memo_id INTEGER NOT NULL,
                tag TEXT NOT NULL,
                tag_key TEXT NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY (memo_id, tag_key),
                FOREIGN KEY (memo_id) REFERENCES book_memos(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_memo_tags_key ON memo_tags (tag_key, memo_id)")
        for table in ("books", "book_chapters", "book_memos"):
            if "version" not in {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        init_write_requests(connection)


def list_books() -> list[dict[str, Any]]:
    init_db()

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT
                books.*,
                COALESCE(memo_counts.memo_count, 0) AS memo_count,
                COALESCE(chapter_counts.chapter_count, 0) AS chapter_count,
                COALESCE(chapter_counts.done_chapter_count, 0) AS done_chapter_count
            FROM books
            LEFT JOIN (
                SELECT book_id, COUNT(*) AS memo_count
                FROM book_memos
                GROUP BY book_id
            ) AS memo_counts ON memo_counts.book_id = books.id
            LEFT JOIN (
                SELECT
                    book_id,
                    COUNT(*) AS chapter_count,
                    COALESCE(SUM(is_done), 0) AS done_chapter_count
                FROM book_chapters
                GROUP BY book_id
            ) AS chapter_counts ON chapter_counts.book_id = books.id
            ORDER BY
                CASE
                    WHEN books.reading_status = '읽는 중' THEN 0
                    WHEN books.reading_status = '읽을 예정' THEN 1
                    WHEN books.progress_percent >= 100 OR books.reading_status = '완료' THEN 2
                    ELSE 3
                END,
                books.updated_at DESC,
                books.id DESC
            """
        ).fetchall()

    return [_with_computed_progress(_row_to_dict(row)) for row in rows]


def list_books_page(page: int, page_size: int = 24, tag: str = "") -> tuple[list[dict[str, Any]], int, int]:
    init_db()

    with _connect() as connection:
        connection.execute("BEGIN")
        tag = tag.strip()
        tag_filter = """WHERE EXISTS (
            SELECT 1 FROM book_memos AS tagged_memos
            JOIN memo_tags ON memo_tags.memo_id = tagged_memos.id
            WHERE tagged_memos.book_id = books.id AND memo_tags.tag_key = ?
        )""" if tag else ""
        parameters: tuple[Any, ...] = (tag.casefold(),) if tag else ()
        total = connection.execute(f"SELECT COUNT(*) FROM books {tag_filter}".strip(), parameters).fetchone()[0]
        last_page = max(1, (total + page_size - 1) // page_size)
        page = min(max(1, page), last_page)
        rows = connection.execute(
            f"""
            WITH selected AS (
                SELECT * FROM books
                {tag_filter}
                ORDER BY
                    CASE
                        WHEN reading_status = '읽는 중' THEN 0
                        WHEN reading_status = '읽을 예정' THEN 1
                        WHEN progress_percent >= 100 OR reading_status = '완료' THEN 2
                        ELSE 3
                    END,
                    updated_at DESC,
                    id DESC
                LIMIT ? OFFSET ?
            )
            SELECT
                selected.*,
                (SELECT COUNT(*) FROM book_memos WHERE book_id = selected.id) AS memo_count,
                (SELECT COUNT(*) FROM book_chapters WHERE book_id = selected.id) AS chapter_count,
                (SELECT COALESCE(SUM(is_done), 0) FROM book_chapters WHERE book_id = selected.id) AS done_chapter_count
            FROM selected
            ORDER BY
                CASE
                    WHEN reading_status = '읽는 중' THEN 0
                    WHEN reading_status = '읽을 예정' THEN 1
                    WHEN progress_percent >= 100 OR reading_status = '완료' THEN 2
                    ELSE 3
                END,
                updated_at DESC,
                id DESC
            """,
            (*parameters, page_size, (page - 1) * page_size),
        ).fetchall()

    return [_with_computed_progress(_row_to_dict(row)) for row in rows], total, page


def create_or_get_book(payload: dict[str, Any]) -> dict[str, Any]:
    init_db()

    isbn = (payload.get("isbn") or payload.get("external_id") or "").strip()

    if not isbn:
        raise ValueError("책 식별값이 없습니다.")

    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO books (
                isbn,
                external_id,
                title,
                authors,
                publisher,
                published_date,
                description,
                thumbnail,
                preview_url,
                source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(isbn) DO UPDATE SET
                external_id = excluded.external_id,
                title = excluded.title,
                authors = excluded.authors,
                publisher = excluded.publisher,
                published_date = excluded.published_date,
                description = excluded.description,
                thumbnail = excluded.thumbnail,
                preview_url = excluded.preview_url,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                isbn,
                payload.get("external_id", ""),
                payload.get("title", "제목 없는 책"),
                payload.get("authors", ""),
                payload.get("publisher", ""),
                payload.get("published_date", ""),
                payload.get("description", ""),
                payload.get("thumbnail", ""),
                payload.get("preview_url", ""),
                payload.get("source", ""),
            ),
        )
        row = connection.execute(
            "SELECT * FROM books WHERE isbn = ?",
            (isbn,),
        ).fetchone()

    return _row_to_dict(row)


def get_book(book_id: int) -> dict[str, Any] | None:
    init_db()

    with _connect() as connection:
        row = connection.execute(
            """
            SELECT
                books.*,
                COALESCE(chapter_counts.chapter_count, 0) AS chapter_count,
                COALESCE(chapter_counts.done_chapter_count, 0) AS done_chapter_count
            FROM books
            LEFT JOIN (
                SELECT
                    book_id,
                    COUNT(*) AS chapter_count,
                    COALESCE(SUM(is_done), 0) AS done_chapter_count
                FROM book_chapters
                GROUP BY book_id
            ) AS chapter_counts ON chapter_counts.book_id = books.id
            WHERE books.id = ?
            """,
            (book_id,),
        ).fetchone()

    if not row:
        return None

    return _with_computed_progress(_row_to_dict(row))


def delete_book(book_id: int) -> bool:
    init_db()

    with _connect() as connection:
        cursor = connection.execute("DELETE FROM books WHERE id = ?", (book_id,))

    return cursor.rowcount > 0


def update_progress(
    book_id: int,
    reading_status: str,
    current_page: int,
    current_chapter: str,
    progress_percent: int,
    expected_version: int | None = None,
    request_id: str = "",
) -> bool:
    init_db()
    if reading_status not in {"읽을 예정", "읽는 중", "완료", "보류"}:
        raise ValueError("읽기 상태가 올바르지 않습니다.")
    progress_percent = max(0, min(progress_percent, 100))
    current_page = max(0, current_page)
    current_chapter = current_chapter.strip()

    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        payload = [book_id, reading_status, current_page, current_chapter, progress_percent, expected_version]
        if replay_result(connection, request_id, "book-progress", payload) is not None:
            return True
        row = connection.execute("SELECT version FROM books WHERE id = ?", (book_id,)).fetchone()
        if row is None:
            return False
        check_version(row, expected_version)
        connection.execute(
            """UPDATE books SET reading_status = ?, current_page = ?, current_chapter = ?,
                progress_percent = ?, version = version + 1, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (reading_status, current_page, current_chapter, progress_percent, book_id),
        )
        record_result(connection, request_id, "book-progress", payload, book_id)
    return True


def list_chapters(book_id: int) -> list[dict[str, Any]]:
    init_db()

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM book_chapters
            WHERE book_id = ?
            ORDER BY position ASC, id ASC
            """,
            (book_id,),
        ).fetchall()

    return [_row_to_dict(row) for row in rows]


def create_chapter(book_id: int, title: str, request_id: str = "") -> None:
    init_db()
    title = title.strip()

    if not title:
        raise ValueError("목차 제목을 입력해주세요.")

    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        payload = [book_id, title]
        if replay_result(connection, request_id, "chapter-create", payload) is not None:
            return
        row = connection.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next_position FROM book_chapters WHERE book_id = ?",
            (book_id,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO book_chapters (book_id, title, position)
            VALUES (?, ?, ?)
            """,
            (book_id, title, row["next_position"]),
        )
        _sync_book_progress(connection, book_id)
        record_result(connection, request_id, "chapter-create", payload, book_id)


def create_chapters(book_id: int, titles: list[str]) -> int:
    init_db()

    cleaned_titles = []
    seen_titles = set()

    for title in titles:
        cleaned_title = " ".join(title.strip().split())

        if cleaned_title and cleaned_title not in seen_titles:
            cleaned_titles.append(cleaned_title)
            seen_titles.add(cleaned_title)

    if not cleaned_titles:
        raise ValueError("추가할 목차를 선택해주세요.")

    with _connect() as connection:
        existing_rows = connection.execute(
            "SELECT title FROM book_chapters WHERE book_id = ?",
            (book_id,),
        ).fetchall()
        existing_titles = {row["title"] for row in existing_rows}
        cleaned_titles = [title for title in cleaned_titles if title not in existing_titles]

        if not cleaned_titles:
            return 0

        row = connection.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next_position FROM book_chapters WHERE book_id = ?",
            (book_id,),
        ).fetchone()
        next_position = row["next_position"]

        connection.executemany(
            """
            INSERT INTO book_chapters (book_id, title, position)
            VALUES (?, ?, ?)
            """,
            [
                (book_id, title, next_position + index)
                for index, title in enumerate(cleaned_titles)
            ],
        )
        _sync_book_progress(connection, book_id)

    return len(cleaned_titles)


def update_chapter(chapter_id: int, is_done: bool, comment: str, expected_version: int | None = None, request_id: str = "") -> int | None:
    init_db()

    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        payload = [chapter_id, is_done, comment.strip(), expected_version]
        replayed = replay_result(connection, request_id, "chapter-edit", payload)
        if replayed is not None:
            return replayed
        row = connection.execute(
            "SELECT book_id, version FROM book_chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()

        if not row:
            return None

        check_version(row, expected_version)
        book_id = row["book_id"]
        connection.execute(
            """
            UPDATE book_chapters
            SET is_done = ?, comment = ?, version = version + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (1 if is_done else 0, comment.strip(), chapter_id),
        )
        _sync_book_progress(connection, book_id)
        record_result(connection, request_id, "chapter-edit", payload, book_id)

    return book_id


def update_chapter_statuses(book_id: int, done_chapter_ids: list[int], expected_versions: dict[int, int] | None = None, request_id: str = "") -> None:
    init_db()
    done_ids = set(done_chapter_ids)

    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        payload = [book_id, sorted(done_ids), expected_versions]
        if replay_result(connection, request_id, "chapter-statuses", payload) is not None:
            return
        rows = connection.execute(
            "SELECT id, version FROM book_chapters WHERE book_id = ?",
            (book_id,),
        ).fetchall()

        if expected_versions is not None:
            if {row["id"] for row in rows} != set(expected_versions):
                raise WriteConflict("목차가 변경되었습니다. 새로고침해 최신 목차를 확인해주세요.")
            for row in rows:
                check_version(row, expected_versions[row["id"]])
        for row in rows:
            connection.execute(
                """
                UPDATE book_chapters
                SET is_done = ?, version = version + 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (1 if row["id"] in done_ids else 0, row["id"]),
            )

        _sync_book_progress(connection, book_id)
        record_result(connection, request_id, "chapter-statuses", payload, book_id)


def update_chapter_comment(chapter_id: int, comment: str, expected_version: int | None = None, request_id: str = "") -> int | None:
    init_db()

    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        payload = [chapter_id, comment.strip(), expected_version]
        replayed = replay_result(connection, request_id, "chapter-comment", payload)
        if replayed is not None:
            return replayed
        row = connection.execute(
            "SELECT book_id, version FROM book_chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()

        if not row:
            return None

        check_version(row, expected_version)
        book_id = row["book_id"]
        connection.execute(
            """
            UPDATE book_chapters
            SET comment = ?, version = version + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (comment.strip(), chapter_id),
        )
        _touch_book(connection, book_id)
        record_result(connection, request_id, "chapter-comment", payload, book_id)

    return book_id


def delete_chapter(chapter_id: int) -> int | None:
    init_db()

    with _connect() as connection:
        row = connection.execute(
            "SELECT book_id FROM book_chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()

        if not row:
            return None

        book_id = row["book_id"]
        connection.execute("DELETE FROM book_chapters WHERE id = ?", (chapter_id,))
        _sync_book_progress(connection, book_id)

    return book_id


def list_memos(book_id: int) -> list[dict[str, Any]]:
    init_db()

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT
                book_memos.*,
                book_chapters.title AS chapter_title
            FROM book_memos
            LEFT JOIN book_chapters ON book_chapters.id = book_memos.chapter_id
            WHERE book_memos.book_id = ?
            ORDER BY book_memos.created_at DESC, book_memos.id DESC
            """,
            (book_id,),
        ).fetchall()

        tags_by_memo = _tags_for_memos(connection, [row["id"] for row in rows])

    return [
        {
            **_row_to_dict(row),
            "tags": tags_by_memo.get(row["id"], []),
            "display_created_at": format_display_datetime(row["created_at"]),
        }
        for row in rows
    ]


def list_available_tags() -> list[str]:
    init_db()
    with _connect() as connection:
        return [row["tag"] for row in connection.execute(
            "SELECT MIN(tag) AS tag FROM memo_tags GROUP BY tag_key ORDER BY tag_key"
        )]


def _tags_for_memos(connection: sqlite3.Connection, memo_ids: list[int]) -> dict[int, list[str]]:
    if not memo_ids:
        return {}
    placeholders = ",".join("?" for _ in memo_ids)
    rows = connection.execute(
        f"SELECT memo_id, tag FROM memo_tags WHERE memo_id IN ({placeholders}) ORDER BY position",
        memo_ids,
    )
    result: dict[int, list[str]] = {}
    for row in rows:
        result.setdefault(row["memo_id"], []).append(row["tag"])
    return result


def _parse_tags(value: str) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for raw in value.split(","):
        tag = " ".join(raw.strip().split())
        if not tag:
            continue
        if len(tag) > 30 or any(unicodedata.category(char).startswith("C") for char in raw):
            raise ValueError("태그는 제어 문자 없이 30자 이내로 입력해주세요.")
        key = tag.casefold()
        if key not in seen:
            tags.append(tag)
            seen.add(key)
    if len(tags) > 5:
        raise ValueError("태그는 최대 5개까지 입력할 수 있습니다.")
    return tags


def update_memo_tags(memo_id: int, tags: str, request_id: str = "") -> int | None:
    parsed = _parse_tags(tags)
    init_db()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        payload = [memo_id, parsed]
        replayed = replay_result(connection, request_id, "memo-tags", payload)
        if replayed is not None:
            return replayed
        row = connection.execute("SELECT book_id FROM book_memos WHERE id = ?", (memo_id,)).fetchone()
        if not row:
            return None
        connection.execute("DELETE FROM memo_tags WHERE memo_id = ?", (memo_id,))
        connection.executemany(
            "INSERT INTO memo_tags (memo_id, tag, tag_key, position) VALUES (?, ?, ?, ?)",
            [(memo_id, tag, tag.casefold(), index) for index, tag in enumerate(parsed)],
        )
        connection.execute("UPDATE book_memos SET version = version + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (memo_id,))
        _touch_book(connection, row["book_id"])
        record_result(connection, request_id, "memo-tags", payload, row["book_id"])
        return row["book_id"]


def search_books_and_memos(query: str, limit: int = 5) -> list[dict[str, Any]]:
    init_db()
    query = query.strip()
    if not query:
        return []

    escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    keyword = f"%{escaped_query}%"
    with _connect() as connection:
        rows = connection.execute(
            """
            WITH candidates AS (
                SELECT
                    books.id AS book_id,
                    books.title AS book_title,
                    books.authors AS authors,
                    books.progress_percent AS progress_percent,
                    books.updated_at AS book_updated_at,
                    book_memos.id AS memo_id,
                    book_memos.title AS memo_title,
                    book_memos.content AS memo_content,
                    book_memos.created_at AS memo_created_at,
                    CASE
                        WHEN books.title = ? COLLATE NOCASE THEN 0
                        WHEN book_memos.title = ? COLLATE NOCASE THEN 1
                        WHEN books.title LIKE ? ESCAPE '\\' THEN 2
                        WHEN book_memos.title LIKE ? ESCAPE '\\' THEN 3
                        WHEN books.authors LIKE ? ESCAPE '\\' THEN 4
                        ELSE 5
                    END AS relevance
                FROM books
                LEFT JOIN book_memos ON book_memos.book_id = books.id
                    AND (book_memos.title LIKE ? ESCAPE '\\' OR book_memos.content LIKE ? ESCAPE '\\')
                WHERE books.title LIKE ? ESCAPE '\\'
                   OR books.authors LIKE ? ESCAPE '\\'
                   OR book_memos.title LIKE ? ESCAPE '\\'
                   OR book_memos.content LIKE ? ESCAPE '\\'
            ), ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY book_id
                    ORDER BY relevance, memo_created_at DESC, memo_id DESC
                ) AS hit_rank FROM candidates
            )
            SELECT book_id, book_title, authors, progress_percent, memo_title, memo_content, relevance
            FROM ranked WHERE hit_rank = 1
            ORDER BY relevance, book_updated_at DESC, memo_created_at DESC, book_id DESC
            LIMIT ?
            """,
            (query, query, keyword, keyword, keyword, keyword, keyword, keyword, keyword, keyword, keyword, limit),
        ).fetchall()

    return [
        {
            "title": row["book_title"] if row["relevance"] in (0, 2, 4) else (row["memo_title"] or row["book_title"]),
            "description": row["book_title"] or f"{row['authors']} · 진행률 {row['progress_percent']}%",
            "snippet": _snippet(row["memo_content"] or "", query=query),
            "meta": f"책 · {row['authors']} · 진행률 {row['progress_percent']}%",
            "url": f"/books/{row['book_id']}",
        }
        for row in rows
    ]


def create_memo(
    book_id: int,
    chapter_id: int | None,
    title: str,
    content: str,
    page: int,
    tags: str = "",
    request_id: str = "",
) -> None:
    init_db()
    parsed_tags = _parse_tags(tags)
    title = title.strip() or "제목 없는 메모"
    content = content.strip()

    if not content:
        raise ValueError("메모 내용을 입력해주세요.")

    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        payload = [book_id, chapter_id, title, content, max(0, page), parsed_tags]
        if replay_result(connection, request_id, "memo-create", payload) is not None:
            return
        book_row = connection.execute(
            "SELECT id FROM books WHERE id = ?",
            (book_id,),
        ).fetchone()
        if not book_row:
            raise ValueError("책을 찾을 수 없습니다.")

        if chapter_id is not None:
            chapter_row = connection.execute(
                "SELECT book_id FROM book_chapters WHERE id = ?",
                (chapter_id,),
            ).fetchone()
            if not chapter_row or chapter_row["book_id"] != book_id:
                raise ValueError("선택한 목차가 책에 속하지 않습니다.")

        cursor = connection.execute(
            """
            INSERT INTO book_memos (book_id, chapter_id, title, content, page)
            VALUES (?, ?, ?, ?, ?)
            """,
            (book_id, chapter_id or None, title, content, max(0, page)),
        )
        connection.executemany(
            "INSERT INTO memo_tags (memo_id, tag, tag_key, position) VALUES (?, ?, ?, ?)",
            [(cursor.lastrowid, tag, tag.casefold(), index) for index, tag in enumerate(parsed_tags)],
        )
        _touch_book(connection, book_id)
        record_result(connection, request_id, "memo-create", payload, cursor.lastrowid)


def update_memo(
    memo_id: int, title: str, content: str, page: int, chapter_id: int | None,
    expected_version: int | None = None, request_id: str = "",
) -> int | None:
    """Edit a saved memo atomically, leaving its tags untouched."""
    title = title.strip() or "제목 없는 메모"
    content = content.strip()
    if not content:
        raise ValueError("메모 내용을 입력해주세요.")
    page = max(0, page)
    init_db()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        payload = [memo_id, title, content, page, chapter_id, expected_version]
        replayed = replay_result(connection, request_id, "memo-edit", payload)
        if replayed is not None:
            return replayed
        memo = connection.execute("SELECT book_id, version FROM book_memos WHERE id = ?", (memo_id,)).fetchone()
        if memo is None:
            return None
        check_version(memo, expected_version)
        if chapter_id is not None:
            chapter = connection.execute("SELECT book_id FROM book_chapters WHERE id = ?", (chapter_id,)).fetchone()
            if chapter is None or chapter["book_id"] != memo["book_id"]:
                raise ValueError("선택한 목차가 책에 속하지 않습니다.")
        connection.execute(
            """UPDATE book_memos SET title = ?, content = ?, page = ?, chapter_id = ?,
               version = version + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (title, content, page, chapter_id, memo_id),
        )
        _touch_book(connection, memo["book_id"])
        record_result(connection, request_id, "memo-edit", payload, memo["book_id"])
        return memo["book_id"]


def delete_memo(memo_id: int) -> int | None:
    init_db()

    with _connect() as connection:
        row = connection.execute(
            "SELECT book_id FROM book_memos WHERE id = ?",
            (memo_id,),
        ).fetchone()

        if not row:
            return None

        book_id = row["book_id"]
        connection.execute("DELETE FROM book_memos WHERE id = ?", (memo_id,))
        _touch_book(connection, book_id)

    return book_id


def _touch_book(connection: sqlite3.Connection, book_id: int) -> None:
    connection.execute(
        "UPDATE books SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (book_id,),
    )


def _sync_book_progress(connection: sqlite3.Connection, book_id: int) -> None:
    row = connection.execute(
        """
        SELECT
            COUNT(*) AS chapter_count,
            COALESCE(SUM(is_done), 0) AS done_chapter_count
        FROM book_chapters
        WHERE book_id = ?
        """,
        (book_id,),
    ).fetchone()

    chapter_count = row["chapter_count"] if row else 0
    done_chapter_count = row["done_chapter_count"] if row else 0
    progress_percent = _calculate_progress_percent(done_chapter_count, chapter_count)

    if chapter_count and done_chapter_count == chapter_count:
        reading_status = "완료"
    elif done_chapter_count:
        reading_status = "읽는 중"
    else:
        reading_status = "읽을 예정"

    connection.execute(
        """
        UPDATE books
        SET
            reading_status = ?,
            progress_percent = ?,
            version = version + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (reading_status, progress_percent, book_id),
    )


def _with_computed_progress(book: dict[str, Any]) -> dict[str, Any]:
    chapter_count = book.get("chapter_count", 0)
    done_chapter_count = book.get("done_chapter_count", 0)

    if not chapter_count:
        return book

    book["progress_percent"] = _calculate_progress_percent(done_chapter_count, chapter_count)

    if chapter_count and done_chapter_count == chapter_count:
        book["reading_status"] = "완료"
    elif done_chapter_count:
        book["reading_status"] = "읽는 중"
    else:
        book["reading_status"] = "읽을 예정"

    return book


def _calculate_progress_percent(done_chapter_count: int, chapter_count: int) -> int:
    if not chapter_count:
        return 0

    return round((done_chapter_count / chapter_count) * 100)


def _snippet(value: str, limit: int = 140, query: str = "") -> str:
    cleaned = " ".join(value.strip().split())
    if len(cleaned) <= limit:
        return cleaned
    match = cleaned.casefold().find(query.casefold()) if query else -1
    start = max(0, match - limit // 3) if match >= 0 else 0
    start = min(start, len(cleaned) - limit)
    end = start + limit
    return f"{'...' if start else ''}{cleaned[start:end].strip()}{'...' if end < len(cleaned) else ''}"


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
