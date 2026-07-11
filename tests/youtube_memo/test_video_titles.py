import importlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests._test_support import prepare_service_import


class YoutubeVideoTitleTests(unittest.TestCase):
    def reload_memo_service(self, tempdir: str):
        prepare_service_import("youtube-memo")
        os.environ["YOUTUBE_MEMO_DB_PATH"] = str(Path(tempdir) / "youtube_memo.sqlite3")
        import app.services.memo_service as memo_service

        return importlib.reload(memo_service)

    def test_database_context_closes_connection_after_use(self):
        with tempfile.TemporaryDirectory() as tempdir:
            memo_service = self.reload_memo_service(tempdir)
            connection = memo_service._connect()
            with connection as open_connection:
                open_connection.execute("SELECT 1")

            with self.assertRaises(sqlite3.ProgrammingError):
                open_connection.execute("SELECT 1")

    def test_create_video_stores_fetched_youtube_title(self):
        with tempfile.TemporaryDirectory() as tempdir:
            memo_service = self.reload_memo_service(tempdir)

            video = memo_service.create_or_get_video(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                title_fetcher=lambda youtube_id, url: "실제 유튜브 제목",
            )

            self.assertEqual(video["title"], "실제 유튜브 제목")

    def test_create_video_uses_fallback_title_when_fetch_fails(self):
        with tempfile.TemporaryDirectory() as tempdir:
            memo_service = self.reload_memo_service(tempdir)

            video = memo_service.create_or_get_video(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                title_fetcher=lambda youtube_id, url: "",
            )

            self.assertEqual(video["title"], "YouTube 영상 dQw4w9WgXcQ")

    def test_existing_fallback_title_is_updated_when_title_is_fetched(self):
        with tempfile.TemporaryDirectory() as tempdir:
            memo_service = self.reload_memo_service(tempdir)

            memo_service.create_or_get_video(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                title_fetcher=lambda youtube_id, url: "",
            )
            video = memo_service.create_or_get_video(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                title_fetcher=lambda youtube_id, url: "나중에 가져온 실제 제목",
            )

            self.assertEqual(video["title"], "나중에 가져온 실제 제목")

    def test_update_memo_changes_title_and_content(self):
        with tempfile.TemporaryDirectory() as tempdir:
            memo_service = self.reload_memo_service(tempdir)
            video = memo_service.create_or_get_video(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                title_fetcher=lambda youtube_id, url: "영상 제목",
            )
            memo = memo_service.create_memo(video["id"], "처음 제목", "처음 내용")

            video_id = memo_service.update_memo(memo["id"], "수정 제목", "수정 내용")
            memos = memo_service.list_memos(video["id"])

            self.assertEqual(video_id, video["id"])
            self.assertEqual(memos[0]["title"], "수정 제목")
            self.assertEqual(memos[0]["content"], "수정 내용")


if __name__ == "__main__":
    unittest.main()
