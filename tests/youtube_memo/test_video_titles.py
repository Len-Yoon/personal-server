import importlib
import os
import tempfile
import unittest
from pathlib import Path


class YoutubeVideoTitleTests(unittest.TestCase):
    def reload_memo_service(self, tempdir: str):
        os.environ["YOUTUBE_MEMO_DB_PATH"] = str(Path(tempdir) / "youtube_memo.sqlite3")
        import app.services.memo_service as memo_service

        return importlib.reload(memo_service)

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


if __name__ == "__main__":
    unittest.main()
