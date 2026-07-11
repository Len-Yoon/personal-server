from app.services.datetime_format import format_news_datetime


def test_formats_utc_iso_as_korean_datetime():
    assert (
        format_news_datetime("2026-07-10T15:58:15.761236+00:00")
        == "2026년 7월 11일 토요일 00:58:15"
    )


def test_formats_rfc822_as_korean_datetime():
    assert (
        format_news_datetime("Fri, 10 Jul 2026 15:45:00 GMT")
        == "2026년 7월 11일 토요일 00:45:00"
    )


def test_returns_original_value_when_unparseable():
    assert format_news_datetime("not-a-date") == "not-a-date"
