import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
import types
from pathlib import Path
from unittest.mock import patch

from tests._test_support import prepare_service_import


class BookMemoServiceTests(unittest.TestCase):
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
