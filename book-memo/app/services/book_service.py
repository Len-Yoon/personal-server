import os
import sqlite3
from pathlib import Path
from typing import Any


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


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with _connect() as connection:
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


def list_books() -> list[dict[str, Any]]:
    init_db()

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT
                books.*,
                COUNT(DISTINCT book_memos.id) AS memo_count,
                COUNT(DISTINCT book_chapters.id) AS chapter_count,
                COALESCE(SUM(book_chapters.is_done), 0) AS done_chapter_count
            FROM books
            LEFT JOIN book_memos ON book_memos.book_id = books.id
            LEFT JOIN book_chapters ON book_chapters.book_id = books.id
            GROUP BY books.id
            ORDER BY books.updated_at DESC, books.id DESC
            """
        ).fetchall()

    return [_row_to_dict(row) for row in rows]


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
            "SELECT * FROM books WHERE id = ?",
            (book_id,),
        ).fetchone()

    if not row:
        return None

    return _row_to_dict(row)


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
) -> None:
    init_db()

    progress_percent = max(0, min(progress_percent, 100))
    current_page = max(0, current_page)

    with _connect() as connection:
        connection.execute(
            """
            UPDATE books
            SET
                reading_status = ?,
                current_page = ?,
                current_chapter = ?,
                progress_percent = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (reading_status, current_page, current_chapter.strip(), progress_percent, book_id),
        )


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


def create_chapter(book_id: int, title: str) -> None:
    init_db()
    title = title.strip()

    if not title:
        raise ValueError("목차 제목을 입력해주세요.")

    with _connect() as connection:
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
        _touch_book(connection, book_id)


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
        _touch_book(connection, book_id)

    return len(cleaned_titles)


def update_chapter(chapter_id: int, is_done: bool, comment: str) -> int | None:
    init_db()

    with _connect() as connection:
        row = connection.execute(
            "SELECT book_id FROM book_chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()

        if not row:
            return None

        book_id = row["book_id"]
        connection.execute(
            """
            UPDATE book_chapters
            SET is_done = ?, comment = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (1 if is_done else 0, comment.strip(), chapter_id),
        )
        _touch_book(connection, book_id)

    return book_id


def update_chapter_statuses(book_id: int, done_chapter_ids: list[int]) -> None:
    init_db()
    done_ids = set(done_chapter_ids)

    with _connect() as connection:
        rows = connection.execute(
            "SELECT id FROM book_chapters WHERE book_id = ?",
            (book_id,),
        ).fetchall()

        for row in rows:
            connection.execute(
                """
                UPDATE book_chapters
                SET is_done = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (1 if row["id"] in done_ids else 0, row["id"]),
            )

        _touch_book(connection, book_id)


def update_chapter_comment(chapter_id: int, comment: str) -> int | None:
    init_db()

    with _connect() as connection:
        row = connection.execute(
            "SELECT book_id FROM book_chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()

        if not row:
            return None

        book_id = row["book_id"]
        connection.execute(
            """
            UPDATE book_chapters
            SET comment = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (comment.strip(), chapter_id),
        )
        _touch_book(connection, book_id)

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
        _touch_book(connection, book_id)

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

    return [_row_to_dict(row) for row in rows]


def create_memo(
    book_id: int,
    chapter_id: int | None,
    title: str,
    content: str,
    page: int,
) -> None:
    init_db()
    title = title.strip() or "제목 없는 메모"
    content = content.strip()

    if not content:
        raise ValueError("메모 내용을 입력해주세요.")

    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO book_memos (book_id, chapter_id, title, content, page)
            VALUES (?, ?, ?, ?, ?)
            """,
            (book_id, chapter_id or None, title, content, max(0, page)),
        )
        _touch_book(connection, book_id)


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


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)
