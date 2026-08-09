import unittest

from tests._test_support import prepare_service_import


prepare_service_import("crawler-worker")
from app.services.nasdaq_relevance import classify_nasdaq_relevance


class NasdaqRelevanceTests(unittest.TestCase):
    def test_classifies_fed_rate_decision_as_alert_with_macro_reason(self):
        """Fails if the macro-event branch stops recognizing rate decisions."""
        result = classify_nasdaq_relevance({"title": "미 연준, 기준금리 동결 결정"})

        self.assertEqual(result["level"], "alert")
        self.assertIn("연준·금리", result["reasons"])

    def test_classifies_fomc_result_announcement_as_alert(self):
        """Fails if a released FOMC result is not treated as a macro event."""
        result = classify_nasdaq_relevance({"title": "FOMC 회의 결과 발표"})

        self.assertEqual(result["level"], "alert")
        self.assertIn("연준·금리", result["reasons"])

    def test_classifies_price_target_change_as_archive(self):
        """Fails if routine analyst price-target commentary becomes an alert."""
        result = classify_nasdaq_relevance({"title": "엔비디아 목표주가 상향"})

        self.assertEqual(result["level"], "archive")

    def test_classifies_fed_rate_outlook_as_archive(self):
        """Fails if a rate outlook is mistaken for an announced policy decision."""
        result = classify_nasdaq_relevance(
            {"title": "연준 의장, 기준금리 동결 가능성 언급"}
        )

        self.assertEqual(result["level"], "archive")

    def test_classifies_rate_decision_as_alert_when_summary_has_future_outlook(self):
        """Fails if a later outlook hides an already announced rate decision."""
        result = classify_nasdaq_relevance(
            {
                "title": "연준, 기준금리 동결 결정",
                "summary": "향후 금리 전망은 다음 회의에서 확인될 예정이다.",
            }
        )

        self.assertEqual(result["level"], "alert")

    def test_classifies_scheduled_cpi_announcement_as_archive(self):
        """Fails if a CPI schedule is mistaken for a released economic result."""
        result = classify_nasdaq_relevance({"title": "미국 CPI 발표 예정"})

        self.assertEqual(result["level"], "archive")

    def test_classifies_semiconductor_export_restriction_as_alert(self):
        """Fails if a semiconductor supply-shock event is not alerted."""
        result = classify_nasdaq_relevance({"title": "반도체 수출 제한 확대 가능성"})

        self.assertEqual(result["level"], "alert")
        self.assertIn("반도체 영향", result["reasons"])

    def test_classifies_macro_result_in_summary_as_alert(self):
        """Fails if classification ignores an article summary."""
        result = classify_nasdaq_relevance(
            {"title": "미국 경제 지표", "summary": "7월 CPI 발표 결과가 시장 예상을 웃돌았다."}
        )

        self.assertEqual(result["level"], "alert")
        self.assertIn("연준·금리", result["reasons"])


if __name__ == "__main__":
    unittest.main()
