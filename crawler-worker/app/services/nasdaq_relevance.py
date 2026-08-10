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
SEMICONDUCTOR_SHOCK_SUBJECT_PATTERN = re.compile(
    r"(?:반도체|칩)", re.IGNORECASE
)
SEMICONDUCTOR_SHOCK_EVENT_PATTERN = re.compile(
    r"(?:수출\s*(?:을|를|이|가)?\s*(?:제한|통제)|제재|공급\s*(?:을|를|이|가)?\s*중단)",
    re.IGNORECASE,
)
SEMICONDUCTOR_SHOCK_OUTLOOK_TERMS = (
    r"(?:가능성|전망|관측|언급|검토|우려|예상|예측|시사|추진|계획|분석)"
)
SEMICONDUCTOR_SHOCK_OUTLOOK_PATTERN = re.compile(
    r"\s*(?:의|을|를|이|가|은|는|에)?\s*"
    r"(?:(?:대한|관한|관련(?:한)?)\s*)?"
    r"(?:(?:확대|강화|도입|시행|추가|재개|장기화)(?:되|될|할)?"
    r"(?:을|를|이|가|은|는)?\s*)?"
    r"(?:될|할)?(?:\s*수\s*있다는?)?"
    r"(?:\s*것(?:으로|이라는?))?\s*"
    + SEMICONDUCTOR_SHOCK_OUTLOOK_TERMS,
    re.IGNORECASE,
)
SEMICONDUCTOR_SHOCK_LEADING_OUTLOOK_PATTERN = re.compile(
    SEMICONDUCTOR_SHOCK_OUTLOOK_TERMS
    + r"(?:\s*중인)?\s*(?:(?::|[-–—])\s*[^,.!?]{0,24})?$",
    re.IGNORECASE,
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
MARKET_SHOCK_SUBJECT_PATTERN = re.compile(
    r"(?:나스닥|미국\s*기술주)", re.IGNORECASE
)
MARKET_SHOCK_EVENT_PATTERN = re.compile(
    r"(?:거래\s*중단|서킷브레이커|폭락|급락)", re.IGNORECASE
)
MARKET_SHOCK_OUTLOOK_PATTERN = re.compile(
    r"\s*(?:발동\s*)?(?:할\s*)?(?:가능성|전망)", re.IGNORECASE
)


def classify_nasdaq_relevance(article: dict) -> dict[str, object]:
    """Classify a news article for alert delivery without mutating the article."""
    if _has_confirmed_macro_result(article):
        return {"level": "alert", "reasons": ["연준·금리"]}
    if _has_confirmed_semiconductor_shock(article):
        return {"level": "alert", "reasons": ["반도체 영향"]}
    if _has_confirmed_market_shock(article):
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


def _has_confirmed_semiconductor_shock(article: dict) -> bool:
    for field in ("title_ko", "title", "summary"):
        text = str(article.get(field) or "").casefold()
        for event_match in SEMICONDUCTOR_SHOCK_EVENT_PATTERN.finditer(text):
            before_event = text[max(0, event_match.start() - 40) : event_match.start()]
            after_event = text[event_match.end() : event_match.end() + 40]
            subject_before = SEMICONDUCTOR_SHOCK_SUBJECT_PATTERN.search(before_event)
            subject_after = SEMICONDUCTOR_SHOCK_SUBJECT_PATTERN.search(after_event)
            if not (subject_before or subject_after):
                continue
            before_subject = (
                before_event[: subject_before.start()]
                if subject_before
                else before_event
            )
            if (
                SEMICONDUCTOR_SHOCK_OUTLOOK_PATTERN.match(after_event)
                or SEMICONDUCTOR_SHOCK_LEADING_OUTLOOK_PATTERN.search(before_event)
                or SEMICONDUCTOR_SHOCK_LEADING_OUTLOOK_PATTERN.search(before_subject)
            ):
                continue
            return True
    return False


def _has_confirmed_market_shock(article: dict) -> bool:
    for field in ("title_ko", "title", "summary"):
        text = str(article.get(field) or "").casefold()
        if not _matches(text, MARKET_SHOCK_PATTERNS):
            continue
        for event_match in MARKET_SHOCK_EVENT_PATTERN.finditer(text):
            before_event = text[max(0, event_match.start() - 40) : event_match.start()]
            after_event = text[event_match.end() : event_match.end() + 40]
            subject_before = MARKET_SHOCK_SUBJECT_PATTERN.search(before_event)
            event_can_precede_subject = event_match.group(0).replace(" ", "") in {
                "거래중단",
                "서킷브레이커",
            }
            subject_after = (
                event_can_precede_subject
                and MARKET_SHOCK_SUBJECT_PATTERN.search(after_event)
            )
            if (
                (subject_before or subject_after)
                and not MARKET_SHOCK_OUTLOOK_PATTERN.match(after_event)
            ):
                return True
    return False
