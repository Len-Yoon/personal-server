import importlib
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from tests._test_support import prepare_service_import


class BookMemoUiContractTests(unittest.TestCase):
    @contextmanager
    def loaded_app(self, tempdir: str):
        """Load the real app with an isolated library database."""
        service_dir = Path(__file__).resolve().parents[2] / "book-memo"
        previous_cwd = Path.cwd()
        previous_db_path = os.environ.get("BOOK_MEMO_DB_PATH")
        os.environ["BOOK_MEMO_DB_PATH"] = str(Path(tempdir) / "book_memo.sqlite3")
        prepare_service_import("book-memo")
        os.chdir(service_dir)
        try:
            import app.main as main

            yield importlib.reload(main).app
        finally:
            os.chdir(previous_cwd)
            if previous_db_path is None:
                os.environ.pop("BOOK_MEMO_DB_PATH", None)
            else:
                os.environ["BOOK_MEMO_DB_PATH"] = previous_db_path

    def test_home_keeps_original_title_book_search_and_portal_return(self):
        """Fails when the original home layout or search route is disconnected."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="app-header"', response.text)
        self.assertIn("<h1>책 메모장</h1>", response.text)
        self.assertIn('<main class="container">', response.text)
        self.assertIn('href="https://len.pe.kr/"', response.text)
        self.assertIn('action="/" method="get"', response.text)
        self.assertIn('name="q"', response.text)
        self.assertIn('class="library-grid"', response.text)

    def test_detail_keeps_original_layout_chapter_memo_and_password_forms(self):
        """Fails when the original detail layout or book CRUD routes are disconnected."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            import app.services.book_service as book_service

            book = book_service.create_or_get_book({"isbn": "9780000000018", "title": "계약 테스트 책"})
            book_service.create_chapter(book["id"], "첫 번째 장")
            chapter = book_service.list_chapters(book["id"])[0]
            book_service.create_memo(book["id"], chapter_id=chapter["id"], title="핵심 메모", content="메모 내용", page=0)
            memo = book_service.list_memos(book["id"])[0]

            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                response = client.get(f"/books/{book['id']}")

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="app-header"', response.text)
        self.assertIn('<main class="container">', response.text)
        self.assertIn(f'action="/books/{book["id"]}/chapters"', response.text)
        self.assertIn(f'action="/books/{book["id"]}/chapters/bulk"', response.text)
        self.assertIn(f'action="/books/{book["id"]}/memos"', response.text)
        self.assertIn(f'action="/books/{book["id"]}/delete"', response.text)
        self.assertIn(f'action="/chapters/{chapter["id"]}"', response.text)
        self.assertIn(f'action="/chapters/{chapter["id"]}/delete"', response.text)
        self.assertIn(f'action="/memos/{memo["id"]}/delete"', response.text)
        self.assertIn('name="delete_password"', response.text)
        self.assertIn('id="toc-fetch-button"', response.text)
        self.assertIn('class="chapter-list"', response.text)
        self.assertIn('class="memo-list"', response.text)

    def test_search_api_keeps_saved_book_and_memo_results_available(self):
        """Fails if a presentation-only change accidentally removes search results."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            import app.services.book_service as book_service

            book = book_service.create_or_get_book({"isbn": "9780000000019", "title": "검색 대상 책"})
            book_service.create_memo(book["id"], chapter_id=None, title="검색 제목", content="검색 가능한 고유 메모 내용", page=0)

            with TestClient(app) as client:
                response = client.get("/api/search", params={"q": "고유 메모"})

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            {"title": "검색 제목", "description": "검색 대상 책", "snippet": "검색 가능한 고유 메모 내용", "meta": "책 ·  · 진행률 0%", "url": f"/books/{book['id']}"},
            response.json()["results"],
        )


if __name__ == "__main__":
    unittest.main()
