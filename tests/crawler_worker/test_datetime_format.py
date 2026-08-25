import unittest
from pathlib import Path

from app.services.datetime_format import format_news_datetime


class DateTimeFormatTests(unittest.TestCase):
    def test_formats_utc_iso_as_compact_kst_datetime(self):
        self.assertEqual(
            format_news_datetime("2026-07-10T15:58:15.761236+00:00"),
            "2026-07-11 00:58",
        )

    def test_formats_rfc822_as_compact_kst_datetime(self):
        self.assertEqual(
            format_news_datetime("Fri, 10 Jul 2026 15:45:00 GMT"),
            "2026-07-11 00:45",
        )

    def test_returns_original_value_when_unparseable(self):
        self.assertEqual(format_news_datetime("not-a-date"), "not-a-date")

    def test_auto_refresh_uses_compact_kst_datetime_without_seconds_or_weekday(self):
        """Fails if the browser refresh path diverges from the displayed time contract."""
        template_path = Path(__file__).resolve().parents[2] / "crawler-worker/app/templates/search.html"
        template = template_path.read_text(encoding="utf-8")
        auto_refresh_script = template.split('<script id="news-auto-refresh">', 1)[1]

        self.assertIn('timeZone: "Asia/Seoul"', auto_refresh_script)
        self.assertIn('month: "2-digit"', auto_refresh_script)
        self.assertIn('day: "2-digit"', auto_refresh_script)
        self.assertIn('hour: "2-digit"', auto_refresh_script)
        self.assertIn('minute: "2-digit"', auto_refresh_script)
        self.assertIn('hourCycle: "h23"', auto_refresh_script)
        self.assertIn(
            'return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;',
            auto_refresh_script,
        )
        self.assertNotIn("weekday:", auto_refresh_script)
        self.assertNotIn("second:", auto_refresh_script)


if __name__ == "__main__":
    unittest.main()
