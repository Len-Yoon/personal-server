import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tests._test_support import prepare_service_import


class BookMemoServiceTests(unittest.TestCase):
    def test_existing_database_gains_tags_without_losing_memos(self):
        with tempfile.TemporaryDirectory() as tempdir:
            service = self.reload_book_service(tempdir)
            service.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(service.DB_PATH) as connection:
                connection.executescript("""
                    CREATE TABLE books (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, isbn TEXT NOT NULL UNIQUE,
                        title TEXT NOT NULL, authors TEXT NOT NULL DEFAULT '',
                        reading_status TEXT NOT NULL DEFAULT '읽는 중',
                        progress_percent INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE book_chapters (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, book_id INTEGER NOT NULL,
                        title TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
                        is_done INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE book_memos (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, book_id INTEGER NOT NULL,
                        chapter_id INTEGER, title TEXT NOT NULL, content TEXT NOT NULL,
                        page INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE
                    );
                    INSERT INTO books (isbn, title) VALUES ('old', '기존 책');
                    INSERT INTO book_memos (book_id, title, content) VALUES (1, '기존 메모', '보존할 내용');
                """)
            service.init_db()
            self.assertEqual(service.list_memos(1)[0]["content"], "보존할 내용")
            self.assertEqual(service.list_memos(1)[0]["tags"], [])

    def test_tags_are_normalized_updated_and_filter_books_before_pagination(self):
        with tempfile.TemporaryDirectory() as tempdir:
            service = self.reload_book_service(tempdir)
            for number in range(26):
                book = service.create_or_get_book({"isbn": f"tag-{number}", "title": f"책 {number}"})
                service.create_memo(book["id"], None, "메모", "내용", 0, tags=" 공부, 공부 , 독서 ")
            first, total, _ = service.list_books_page(1, tag="공부")
            second, _, _ = service.list_books_page(2, tag="공부")
            self.assertEqual((len(first), len(second), total), (24, 2, 26))
            self.assertEqual(service.list_memos(first[0]["id"])[0]["tags"], ["공부", "독서"])
            self.assertEqual(service.list_available_tags(), ["공부", "독서"])
            memo_id = service.list_memos(first[0]["id"])[0]["id"]
            self.assertEqual(service.update_memo_tags(memo_id, "새 태그"), first[0]["id"])
            self.assertEqual(service.list_memos(first[0]["id"])[0]["tags"], ["새 태그"])
            self.assertEqual(service.list_books_page(1, tag="공부")[1], 25)
            self.assertEqual(service.update_memo_tags(999999, "기타"), None)

    def test_tags_reject_overlong_control_and_excess_count_without_partial_write(self):
        with tempfile.TemporaryDirectory() as tempdir:
            service = self.reload_book_service(tempdir)
            book = service.create_or_get_book({"isbn": "invalid-tags", "title": "책"})
            for tags in ["x" * 31, "a\x00b", "a,b,c,d,e,f"]:
                with self.assertRaises(ValueError):
                    service.create_memo(book["id"], None, "메모", "내용", 0, tags=tags)
            self.assertEqual(service.list_memos(book["id"]), [])

    def test_search_ranks_title_before_content_and_shows_query_context(self):
        with tempfile.TemporaryDirectory() as tempdir:
            service = self.reload_book_service(tempdir)
            book = service.create_or_get_book({"isbn": "rank", "title": "검색 책"})
            other = service.create_or_get_book({"isbn": "rank-other", "title": "다른 책"})
            service.create_memo(other["id"], None, "일반", "앞" * 100 + "핵심단어" + "뒤" * 100, 0)
            service.create_memo(book["id"], None, "핵심단어 제목", "짧은 내용", 0)
            results = service.search_books_and_memos("핵심단어")
            self.assertEqual(results[0]["title"], "핵심단어 제목")
            self.assertIn("핵심단어", results[1]["snippet"])
            self.assertEqual(set(results[0]), {"title", "description", "snippet", "meta", "url"})

    def test_search_treats_like_wildcards_as_text_and_returns_one_parent_result(self):
        with tempfile.TemporaryDirectory() as tempdir:
            service = self.reload_book_service(tempdir)
            book = service.create_or_get_book({"isbn": "wildcard", "title": "100% 독서"})
            service.create_memo(book["id"], None, "메모", "관련 없는 글", 0)
            service.create_memo(book["id"], None, "다른 메모", "또 다른 글", 0)
            self.assertEqual(len(service.search_books_and_memos("100%")), 1)
            self.assertEqual(service.search_books_and_memos("%") [0]["title"], "100% 독서")
            self.assertEqual(service.search_books_and_memos("_"), [])

    def test_search_limits_to_best_memo_per_book_before_global_limit(self):
        with tempfile.TemporaryDirectory() as tempdir:
            service = self.reload_book_service(tempdir)
            first = service.create_or_get_book({"isbn": "many", "title": "첫 책"})
            second = service.create_or_get_book({"isbn": "second", "title": "둘째 책"})
            for number in range(4):
                service.create_memo(first["id"], None, f"메모 {number}", "공통단어", 0)
            service.create_memo(second["id"], None, "다른 메모", "공통단어", 0)
            with service._connect() as connection:
                connection.execute("UPDATE books SET updated_at = '2099-01-01' WHERE id = ?", (first["id"],))
                connection.execute("UPDATE books SET updated_at = '2000-01-01' WHERE id = ?", (second["id"],))
            results = service.search_books_and_memos("공통단어", limit=2)
            self.assertEqual(len(results), 2)
            self.assertEqual({item["url"] for item in results}, {f"/books/{first['id']}", f"/books/{second['id']}"})
            self.assertEqual(results[0]["url"], f"/books/{first['id']}")

    def test_search_ranks_exact_memo_title_before_partial_book_title(self):
        with tempfile.TemporaryDirectory() as tempdir:
            service = self.reload_book_service(tempdir)
            partial = service.create_or_get_book({"isbn": "partial", "title": "Alpha and more"})
            exact = service.create_or_get_book({"isbn": "exact", "title": "다른 책"})
            service.create_memo(exact["id"], None, "ALPHA", "내용", 0)
            with service._connect() as connection:
                connection.execute("UPDATE books SET updated_at = '2099-01-01' WHERE id = ?", (partial["id"],))
            results = service.search_books_and_memos("alpha", limit=2)
            self.assertEqual(results[0]["url"], f"/books/{exact['id']}")

    def test_search_displays_book_title_when_book_or_author_is_best_match(self):
        with tempfile.TemporaryDirectory() as tempdir:
            service = self.reload_book_service(tempdir)
            exact = service.create_or_get_book({"isbn": "book-exact", "title": "Alpha", "authors": "A"})
            partial = service.create_or_get_book({"isbn": "book-partial", "title": "Alpha guide", "authors": "B"})
            author = service.create_or_get_book({"isbn": "book-author", "title": "Other", "authors": "Alpha writer"})
            for book in (exact, partial):
                service.create_memo(book["id"], None, "Alpha note", "Alpha in content", 0)
            service.create_memo(author["id"], None, "Other note", "Alpha in content", 0)
            results = service.search_books_and_memos("Alpha", limit=3)
            titles = {item["url"]: item["title"] for item in results}
            self.assertEqual(titles[f"/books/{exact['id']}"], "Alpha")
            self.assertEqual(titles[f"/books/{partial['id']}"], "Alpha guide")
            self.assertEqual(titles[f"/books/{author['id']}"], "Other")

    def test_tag_filter_ignores_case_consistent_with_duplicate_normalization(self):
        with tempfile.TemporaryDirectory() as tempdir:
            service = self.reload_book_service(tempdir)
            book = service.create_or_get_book({"isbn": "case-tag", "title": "책"})
            service.create_memo(book["id"], None, "메모", "내용", 0, tags="Study, study, Straße, STRASSE")
            self.assertEqual(service.list_memos(book["id"])[0]["tags"], ["Study", "Straße"])
            self.assertEqual(service.list_books_page(1, tag="STUDY")[1], 1)
            self.assertEqual(service.list_books_page(1, tag="STRASSE")[1], 1)

    def test_deleting_memo_cascades_tags_and_invalid_update_preserves_old_tags(self):
        with tempfile.TemporaryDirectory() as tempdir:
            service = self.reload_book_service(tempdir)
            book = service.create_or_get_book({"isbn": "cascade", "title": "책"})
            service.create_memo(book["id"], None, "메모", "내용", 0, tags="보존")
            memo_id = service.list_memos(book["id"])[0]["id"]
            with self.assertRaises(ValueError):
                service.update_memo_tags(memo_id, "x" * 31)
            self.assertEqual(service.list_memos(book["id"])[0]["tags"], ["보존"])
            self.assertEqual(service.delete_memo(memo_id), book["id"])
            self.assertEqual(service.list_available_tags(), [])

    def reload_book_service(self, tempdir: str):
        prepare_service_import("book-memo")
        os.environ["BOOK_MEMO_DB_PATH"] = str(Path(tempdir) / "book_memo.sqlite3")
        import app.services.book_service as book_service

        return importlib.reload(book_service)

    def test_database_context_closes_connection_after_use(self):
        with tempfile.TemporaryDirectory() as tempdir:
            book_service = self.reload_book_service(tempdir)
            connection = book_service._connect()
            with connection as open_connection:
                open_connection.execute("SELECT 1")

            with self.assertRaises(sqlite3.ProgrammingError):
                open_connection.execute("SELECT 1")

    def reload_book_search(self):
        prepare_service_import("book-memo")
        fake_requests = types.ModuleType("requests")
        fake_requests.RequestException = Exception
        fake_requests.get = lambda *args, **kwargs: None
        sys.modules.setdefault("requests", fake_requests)
        import app.services.book_search as book_search

        return importlib.reload(book_search)

    def tearDown(self):
        sys.modules.pop("requests", None)

    def test_create_chapters_deduplicates_blank_titles(self):
        with tempfile.TemporaryDirectory() as tempdir:
            book_service = self.reload_book_service(tempdir)
            book = book_service.create_or_get_book({"isbn": "9780000000001", "title": "샘플 책"})

            added = book_service.create_chapters(book["id"], ["  개요  ", "개요", "", "요약", "요약"])
            chapters = book_service.list_chapters(book["id"])

            self.assertEqual(added, 2)
            self.assertEqual([chapter["title"] for chapter in chapters], ["개요", "요약"])

    def test_list_books_prioritizes_reading_books_then_planned_then_completed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            book_service = self.reload_book_service(tempdir)

            reading_book = book_service.create_or_get_book({"isbn": "9780000000003", "title": "읽는 중 책"})
            planned_book = book_service.create_or_get_book({"isbn": "9780000000004", "title": "읽을 예정 책"})
            finished_book = book_service.create_or_get_book({"isbn": "9780000000005", "title": "완료 책"})

            book_service.update_progress(
                reading_book["id"],
                reading_status="읽는 중",
                current_page=120,
                current_chapter="3장",
                progress_percent=45,
            )
            book_service.update_progress(
                planned_book["id"],
                reading_status="읽을 예정",
                current_page=0,
                current_chapter="",
                progress_percent=0,
            )
            book_service.update_progress(
                finished_book["id"],
                reading_status="완료",
                current_page=300,
                current_chapter="",
                progress_percent=100,
            )

            books = book_service.list_books()

            self.assertEqual([book["title"] for book in books], ["읽는 중 책", "읽을 예정 책", "완료 책"])

    def test_book_page_is_bounded_stable_and_clamped(self):
        with tempfile.TemporaryDirectory() as tempdir:
            book_service = self.reload_book_service(tempdir)
            book_service.init_db()
            with book_service._connect() as connection:
                connection.executemany(
                    "INSERT INTO books (isbn, title, reading_status, updated_at) VALUES (?, ?, ?, ?)",
                    [(f"isbn-{number}", f"책 {number}", "읽는 중", "2026-01-01 00:00:00") for number in range(26)],
                )
                connection.execute("UPDATE books SET reading_status = '읽을 예정' WHERE isbn = 'isbn-25'")
            first, total, page = book_service.list_books_page(1)
            second, _, second_page = book_service.list_books_page(2)
            beyond, _, beyond_page = book_service.list_books_page(999)

            self.assertEqual((total, page, second_page, beyond_page), (26, 1, 2, 2))
            self.assertEqual(len(first), 24)
            self.assertEqual([book["title"] for book in second], ["책 0", "책 25"])
            self.assertEqual([book["title"] for book in beyond], ["책 0", "책 25"])

    def test_book_page_uses_one_snapshot_when_last_row_is_deleted_after_count(self):
        with tempfile.TemporaryDirectory() as tempdir:
            book_service = self.reload_book_service(tempdir)
            book_service.init_db()
            with book_service._connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executemany(
                    "INSERT INTO books (isbn, title) VALUES (?, ?)",
                    [(f"isbn-{number}", f"책 {number}") for number in range(25)],
                )

            original_connect = book_service._connect
            deleted = False

            class RacingConnection:
                def __init__(self, connection):
                    self.connection = connection

                def execute(self, sql, parameters=()):
                    nonlocal deleted
                    cursor = self.connection.execute(sql, parameters)
                    if sql == "SELECT COUNT(*) FROM books" and not deleted:
                        deleted = True
                        with sqlite3.connect(book_service.DB_PATH) as writer:
                            writer.execute("DELETE FROM books WHERE isbn = 'isbn-0'")
                    return cursor

            @contextmanager
            def racing_connect():
                with original_connect() as connection:
                    yield RacingConnection(connection)

            with patch.object(book_service, "_connect", racing_connect):
                rows, total, page = book_service.list_books_page(2)

            self.assertTrue(deleted)
            self.assertEqual((total, page, [book["title"] for book in rows]), (25, 2, ["책 0"]))

    def test_home_indexes_serve_sort_and_page_counts(self):
        with tempfile.TemporaryDirectory() as tempdir:
            book_service = self.reload_book_service(tempdir)
            book_service.init_db()
            with book_service._connect() as connection:
                sort_plan = " ".join(row[3] for row in connection.execute(
                    """EXPLAIN QUERY PLAN SELECT id FROM books ORDER BY
                    CASE WHEN reading_status = '읽는 중' THEN 0
                    WHEN reading_status = '읽을 예정' THEN 1
                    WHEN progress_percent >= 100 OR reading_status = '완료' THEN 2
                    ELSE 3 END, updated_at DESC, id DESC LIMIT 24"""
                ))
                chapter_plan = " ".join(row[3] for row in connection.execute(
                    "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM book_chapters WHERE book_id = 1"
                ))
                memo_plan = " ".join(row[3] for row in connection.execute(
                    "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM book_memos WHERE book_id = 1"
                ))
            self.assertIn("USING INDEX idx_books_home_order", sort_plan)
            self.assertNotIn("TEMP B-TREE", sort_plan)
            self.assertIn("USING COVERING INDEX idx_book_chapters_book", chapter_plan)
            self.assertIn("USING COVERING INDEX idx_book_memos_book", memo_plan)

    def test_get_and_list_preserve_manual_progress_without_chapters(self):
        with tempfile.TemporaryDirectory() as tempdir:
            book_service = self.reload_book_service(tempdir)
            book = book_service.create_or_get_book({"isbn": "9780000000006", "title": "수동 진행 책"})
            book_service.update_progress(
                book["id"],
                reading_status="보류",
                current_page=140,
                current_chapter="중간",
                progress_percent=35,
            )
            fixed_timestamp = "2020-01-02 03:04:05"
            with book_service._connect() as connection:
                connection.execute(
                    "UPDATE books SET updated_at = ? WHERE id = ?",
                    (fixed_timestamp, book["id"]),
                )
                before = connection.execute(
                    "SELECT * FROM books WHERE id = ?", (book["id"],)
                ).fetchone()

            self.assertEqual(book_service.get_book(book["id"])["progress_percent"], 35)
            self.assertEqual(book_service.get_book(book["id"])["reading_status"], "보류")
            self.assertEqual(book_service.list_books()[0]["progress_percent"], 35)
            self.assertEqual(book_service.list_books()[0]["reading_status"], "보류")

            with book_service._connect() as connection:
                after = connection.execute(
                    "SELECT * FROM books WHERE id = ?", (book["id"],)
                ).fetchone()
            self.assertEqual(dict(after), dict(before))

    def test_get_and_list_compute_progress_from_chapters_without_writing(self):
        with tempfile.TemporaryDirectory() as tempdir:
            book_service = self.reload_book_service(tempdir)
            book = book_service.create_or_get_book({"isbn": "9780000000007", "title": "목차 진행 책"})
            book_service.create_chapters(book["id"], ["1장", "2장", "3장"])
            chapters = book_service.list_chapters(book["id"])
            with book_service._connect() as connection:
                connection.execute(
                    "UPDATE book_chapters SET is_done = 1 WHERE id IN (?, ?)",
                    (chapters[0]["id"], chapters[1]["id"]),
                )
                connection.execute(
                    "UPDATE books SET progress_percent = 99, reading_status = '보류', updated_at = ? WHERE id = ?",
                    ("2020-01-02 03:04:05", book["id"]),
                )
                before = connection.execute(
                    "SELECT * FROM books WHERE id = ?", (book["id"],)
                ).fetchone()

            listed = book_service.list_books()[0]
            fetched = book_service.get_book(book["id"])
            self.assertEqual(listed["progress_percent"], 67)
            self.assertEqual(fetched["progress_percent"], 67)
            self.assertEqual(listed["reading_status"], "읽는 중")
            self.assertEqual(fetched["reading_status"], "읽는 중")

            with book_service._connect() as connection:
                after = connection.execute(
                    "SELECT * FROM books WHERE id = ?", (book["id"],)
                ).fetchone()
            self.assertEqual(dict(after), dict(before))

    def test_create_memo_rejects_chapter_from_another_book_without_side_effect(self):
        with tempfile.TemporaryDirectory() as tempdir:
            book_service = self.reload_book_service(tempdir)
            book = book_service.create_or_get_book({"isbn": "9780000000008", "title": "메모 책"})
            other_book = book_service.create_or_get_book({"isbn": "9780000000009", "title": "다른 책"})
            book_service.create_chapter(other_book["id"], "다른 장")
            chapter = book_service.list_chapters(other_book["id"])[0]

            with self.assertRaises(ValueError):
                book_service.create_memo(
                    book["id"], chapter_id=chapter["id"], title="잘못된 메모", content="내용", page=1
                )

            self.assertEqual(book_service.list_memos(book["id"]), [])

    def test_create_memo_rejects_missing_chapter_without_side_effect(self):
        with tempfile.TemporaryDirectory() as tempdir:
            book_service = self.reload_book_service(tempdir)
            book = book_service.create_or_get_book({"isbn": "9780000000010", "title": "없는 장 책"})

            with self.assertRaises(ValueError):
                book_service.create_memo(
                    book["id"], chapter_id=99999, title="잘못된 메모", content="내용", page=1
                )

            self.assertEqual(book_service.list_memos(book["id"]), [])

    def test_search_books_falls_back_to_google_books(self):
        os.environ.pop("ALADIN_TTB_KEY", None)
        book_search = self.reload_book_search()

        google_result = [
            {
                "external_id": "google-1",
                "isbn": "9780000000002",
                "title": "Google 책",
                "authors": "홍길동",
                "publisher": "테스트 출판사",
                "published_date": "2026",
                "description": "설명",
                "thumbnail": "https://example.com/thumb.jpg",
                "preview_url": "https://example.com/preview",
                "source": "google_books",
            }
        ]

        with patch.object(book_search, "_search_aladin", return_value=[]), patch.object(
            book_search, "_search_google_books", return_value=google_result
        ), patch.object(book_search, "_search_open_library", return_value=[]):
            books = book_search.search_books("샘플")

        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["source"], "google_books")
        self.assertEqual(books[0]["isbn"], "9780000000002")
        self.assertEqual(books[0]["thumbnail"], "https://example.com/thumb.jpg")


if __name__ == "__main__":
    unittest.main()
