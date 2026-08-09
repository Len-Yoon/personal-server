from __future__ import annotations

import re


MACRO_PATTERNS = (
    re.compile(
        r"(?:연준|\bfed\b|\bfomc\b).{0,40}(?:기준\s*금리|금리).{0,30}(?:동결|인상|인하|결정|발표)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:기준\s*금리|금리).{0,30}(?:동결|인상|인하|결정|발표).{0,40}(?:연준|\bfed\b|\bfomc\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:연준|\bfed\b|\bfomc\b).{0,40}(?:결정|결과|성명(?:서)?|발표)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:\bcpi\b|\bpce\b|소비자\s*물가|개인소비지출).{0,40}(?:발표|결과|수치)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:비농업\s*고용|실업률).{0,40}(?:발표|결과|수치|증가|감소|상승|하락)",
        re.IGNORECASE,
    ),
)
MACRO_OUTLOOK_PATTERNS = (
    re.compile(r"가능성|전망|관측|언급", re.IGNORECASE),
)
MACRO_SCHEDULE_PATTERNS = (
    re.compile(r"발표\s*(?:예정|전|대기|일정)|발표를?\s*앞두고|예정", re.IGNORECASE),
)
SEMICONDUCTOR_SHOCK_PATTERNS = (
    re.compile(
        r"(?:반도체|칩).{0,40}(?:수출\s*제한|수출\s*통제|제재|공급\s*중단)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:수출\s*제한|수출\s*통제|제재|공급\s*중단).{0,40}(?:반도체|칩)",
        re.IGNORECASE,
    ),
)
MARKET_SHOCK_PATTERNS = (
    re.compile(
        r"(?:나스닥|미국\s*기술주).{0,40}(?:거래\s*중단|서킷브레이커|폭락|급락)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:거래\s*중단|서킷브레이커).{0,40}(?:나스닥|미국\s*기술주)",
        re.IGNORECASE,
    ),
)


def classify_nasdaq_relevance(article: dict) -> dict[str, object]:
    """Classify a news article for alert delivery without mutating the article."""
    text = " ".join(
        str(article.get(key) or "")
        for key in ("title_ko", "title", "summary")
    ).casefold()
    if _has_confirmed_macro_result(article):
        return {"level": "alert", "reasons": ["연준·금리"]}
    if _matches(text, SEMICONDUCTOR_SHOCK_PATTERNS):
        return {"level": "alert", "reasons": ["반도체 영향"]}
    if _matches(text, MARKET_SHOCK_PATTERNS):
        return {"level": "alert", "reasons": ["미국 기술주 시장 영향"]}
    return {"level": "archive", "reasons": []}


def _matches(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _has_confirmed_macro_result(article: dict) -> bool:
    for field in ("title_ko", "title", "summary"):
        text = str(article.get(field) or "").casefold()
        if (
            _matches(text, MACRO_PATTERNS)
            and not _matches(text, MACRO_OUTLOOK_PATTERNS)
            and not _matches(text, MACRO_SCHEDULE_PATTERNS)
        ):
            return True
    return False
