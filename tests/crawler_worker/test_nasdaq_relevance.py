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

    def test_classifies_possible_semiconductor_export_restriction_as_archive(self):
        """Fails if a possible export restriction is treated as a confirmed event."""
        result = classify_nasdaq_relevance({"title": "반도체 수출 제한 확대 가능성"})

        self.assertEqual(result["level"], "archive")

    def test_classifies_semiconductor_shock_outlook_as_archive(self):
        """Fails if forecasts or commentary trigger semiconductor alerts."""
        for title in (
            "반도체 공급 중단 전망",
            "미국, 칩 수출 통제 가능성 언급",
            "반도체 수출 제한 강화 검토",
            "전문가 전망: 반도체 수출 제한 확대",
            "검토 중인 반도체 수출 제한",
            "반도체 수출 제한 시행을 검토",
            "반도체 수출 제한 관련 언급",
            "반도체 수출 제한에 대한 전망",
            "반도체 수출 제한 분석",
            "전망: 미국 반도체 수출 제한 확대",
            "반도체 공급이 중단될 것으로 예상",
            "미국, 반도체 수출이 제한될 것으로 전망",
            "칩 수출 통제가 강화될 것으로 관측",
            "반도체 공급 중단될 것이라는 우려",
        ):
            with self.subTest(title=title):
                result = classify_nasdaq_relevance({"title": title})

                self.assertEqual(result["level"], "archive")

    def test_classifies_confirmed_semiconductor_shocks_as_alert(self):
        """Fails if confirmed export restrictions or supply stops are not alerted."""
        for title in (
            "미국, 반도체 수출 제한 시행",
            "공장 화재로 칩 공급 중단",
            "미국이 반도체 수출을 제한했다",
            "화재로 반도체 공급이 중단됐다",
        ):
            with self.subTest(title=title):
                result = classify_nasdaq_relevance({"title": title})

                self.assertEqual(result["level"], "alert")
                self.assertIn("반도체 영향", result["reasons"])

    def test_classifies_confirmed_semiconductor_shock_with_unrelated_outlook_as_alert(self):
        """Fails if unrelated outlook wording hides a confirmed semiconductor event."""
        result = classify_nasdaq_relevance(
            {
                "title": "미국, 반도체 수출 제한 시행",
                "summary": "향후 기술주 시장 전망은 불투명하다.",
            }
        )

        self.assertEqual(result["level"], "alert")
        self.assertIn("반도체 영향", result["reasons"])

    def test_classifies_nasdaq_crash_possibility_as_archive(self):
        """Fails if possible Nasdaq declines are mistaken for confirmed events."""
        result = classify_nasdaq_relevance({"title": "나스닥 급락 가능성 커져"})

        self.assertEqual(result["level"], "archive")

    def test_classifies_us_tech_crash_outlook_as_archive(self):
        """Fails if a US tech crash outlook is mistaken for a confirmed event."""
        result = classify_nasdaq_relevance({"title": "미국 기술주 폭락 전망"})

        self.assertEqual(result["level"], "archive")

    def test_classifies_circuit_breaker_activation_outlook_as_archive(self):
        """Fails if activation wording separates a circuit breaker from its qualifier."""
        for title in (
            "나스닥 서킷브레이커 발동 가능성",
            "나스닥 서킷브레이커 발동 전망",
        ):
            with self.subTest(title=title):
                result = classify_nasdaq_relevance({"title": title})

                self.assertEqual(result["level"], "archive")

    def test_classifies_confirmed_market_shocks_as_alert(self):
        """Fails if confirmed crashes or circuit breakers stop producing alerts."""
        for title in (
            "나스닥 급락",
            "미국 기술주 폭락",
            "나스닥 서킷브레이커 발동",
        ):
            with self.subTest(title=title):
                result = classify_nasdaq_relevance({"title": title})

                self.assertEqual(result["level"], "alert")
                self.assertIn("미국 기술주 시장 영향", result["reasons"])

    def test_classifies_confirmed_market_shock_with_unrelated_outlook_as_alert(self):
        """Fails if unrelated outlook wording hides a confirmed market shock."""
        for title in (
            "나스닥 서킷브레이커 발동, 향후 시장 전망은 불투명",
            "나스닥 급락, 연준 금리 인하 가능성 고조",
        ):
            with self.subTest(title=title):
                result = classify_nasdaq_relevance({"title": title})

                self.assertEqual(result["level"], "alert")
                self.assertIn("미국 기술주 시장 영향", result["reasons"])

    def test_classifies_confirmed_title_when_summary_only_has_outlook_as_alert(self):
        """Fails if outlook in another field hides a confirmed market shock."""
        result = classify_nasdaq_relevance(
            {
                "title": "나스닥 서킷브레이커 발동",
                "summary": "향후 시장 전망은 여전히 불투명하다.",
            }
        )

        self.assertEqual(result["level"], "alert")
        self.assertIn("미국 기술주 시장 영향", result["reasons"])

    def test_classifies_confirmed_shock_after_outlook_shock_as_alert(self):
        """Fails if one qualified shock hides a later confirmed occurrence."""
        result = classify_nasdaq_relevance(
            {"title": "나스닥 급락 가능성 경고 뒤 실제 급락"}
        )

        self.assertEqual(result["level"], "alert")
        self.assertIn("미국 기술주 시장 영향", result["reasons"])

    def test_classifies_macro_result_in_summary_as_alert(self):
        """Fails if classification ignores an article summary."""
        result = classify_nasdaq_relevance(
            {"title": "미국 경제 지표", "summary": "7월 CPI 발표 결과가 시장 예상을 웃돌았다."}
        )

        self.assertEqual(result["level"], "alert")
        self.assertIn("연준·금리", result["reasons"])

    def test_classifies_confirmed_market_close_as_alert(self):
        """Fails if completed US market closes stop reaching Telegram."""
        result = classify_nasdaq_relevance(
            {"title": "뉴욕증시 마감, 나스닥 1.2% 상승"}
        )

        self.assertEqual(result["level"], "alert")
        self.assertIn("미국 증시 마감", result["reasons"])

    def test_classifies_confirmed_exchange_rate_and_oil_moves_as_alert(self):
        """Fails if completed FX and oil market moves remain archive-only."""
        for title, reason in (
            ("원/달러 환율 1,380원에 마감", "환율"),
            ("국제유가 상승 마감, WTI 2% 올라", "유가"),
        ):
            with self.subTest(title=title):
                result = classify_nasdaq_relevance({"title": title})

                self.assertEqual(result["level"], "alert")
                self.assertIn(reason, result["reasons"])

    def test_classifies_confirmed_major_semiconductor_stock_move_as_alert(self):
        """Fails if completed major semiconductor stock moves remain archive-only."""
        result = classify_nasdaq_relevance(
            {"title": "엔비디아 실적 발표 후 주가 8% 급등"}
        )

        self.assertEqual(result["level"], "alert")
        self.assertIn("반도체 대형주", result["reasons"])

    def test_keeps_market_outlooks_and_routine_stock_opinions_as_archive(self):
        """Fails if forecasts or target-price opinions create routine alerts."""
        for title in (
            "뉴욕증시 상승 전망",
            "원/달러 환율 상승 가능성",
            "국제유가 추가 상승 전망",
            "엔비디아 목표주가 상향",
        ):
            with self.subTest(title=title):
                result = classify_nasdaq_relevance({"title": title})

                self.assertEqual(result["level"], "archive")


if __name__ == "__main__":
    unittest.main()
