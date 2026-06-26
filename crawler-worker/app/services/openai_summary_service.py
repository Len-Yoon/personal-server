import json
import os
from typing import Any

from openai import OpenAI


def _get_client():
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        return None

    return OpenAI(api_key=api_key)


def summarize_market_news(article: dict[str, Any]) -> dict[str, Any]:
    client = _get_client()

    if not client:
        return {
            "ok": False,
            "error": "OPENAI_API_KEY is not configured",
        }

    model = os.getenv("OPENAI_SUMMARY_MODEL", "gpt-5-mini")

    title = article.get("title_original") or article.get("title", "")
    title_ko = article.get("title_ko", "")
    source = article.get("source", "")
    published_at = article.get("published_at", "")
    category = article.get("category", "")
    url = article.get("url", "")

    prompt = f"""
아래 뉴스는 해외 시장/선물 매매 참고용 뉴스다.

너는 나스닥 선물, 금 선물, 홍콩50, 글로벌 매크로 뉴스를 분석하는 보조자다.
기사 전문이 아니라 뉴스 제목/출처/발행시간만 제공되므로 과장하지 말고, 제목에서 판단 가능한 범위만 분석하라.

출력은 반드시 JSON만 반환하라.
마크다운 코드블록은 쓰지 마라.

JSON 형식:
{{
  "title_ko": "한국어 뉴스 제목",
  "summary": "핵심 요약 2문장",
  "market_impact": "POSITIVE | NEGATIVE | NEUTRAL | UNCERTAIN",
  "importance": "HIGH | MEDIUM | LOW",
  "related_markets": ["WORLD", "NASDAQ", "GOLD", "HK50"],
  "keywords": ["키워드1", "키워드2", "키워드3"],
  "trading_note": "매매 참고 코멘트 1~2문장",
  "caution": "주의할 점 1문장"
}}

뉴스 정보:
- category: {category}
- title: {title}
- title_ko: {title_ko}
- source: {source}
- published_at: {published_at}
- url: {url}
""".strip()

    try:
        response = client.responses.create(
            model=model,
            input=prompt,
        )

        text = response.output_text.strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = {
                "summary": text,
                "market_impact": "UNCERTAIN",
                "importance": "LOW",
                "related_markets": [category] if category else [],
                "keywords": [],
                "trading_note": "JSON 파싱에 실패했으므로 원문 응답을 확인해야 합니다.",
                "caution": "모델 응답 형식이 예상과 다릅니다.",
            }

        return {
            "ok": True,
            "model": model,
            "article": article,
            "summary": parsed,
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }
