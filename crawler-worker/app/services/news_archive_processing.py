import re
from typing import Any


EVENT_MARKER_PATTERN = re.compile(
    r"cpi|pce|fomc|fed|연준|고용|실업률|gdp|wti|브렌트|opec|나스닥|nasdaq",
    re.IGNORECASE,
)


def market_topic(article: dict[str, Any]) -> str:
    text = f"{article.get('title_ko') or article.get('title') or ''} {article.get('summary') or ''}".casefold()
    if re.search(r"나스닥|nasdaq", text):
        return "나스닥"
    if re.search(r"원유|유가|국제유가|wti|브렌트|brent|opec", text):
        return "원유"
    if re.search(r"금\s*(?:값|가격|선물|시세|시장)|골드|gold|xau", text):
        return "금"
    return "미국"


def same_market_event(current: dict[str, Any], previous: dict[str, Any]) -> bool:
    if market_topic(current) != market_topic(previous):
        return False
    if has_new_market_detail(current, previous):
        return False
    current_key = headline_key(current)
    previous_key = headline_key(previous)
    if not current_key or not previous_key:
        return False
    if current_key == previous_key or current_key in previous_key or previous_key in current_key:
        return True
    similarity = headline_bigram_similarity(current_key, previous_key)
    shared_markers = set(EVENT_MARKER_PATTERN.findall(current_key)) & set(
        EVENT_MARKER_PATTERN.findall(previous_key)
    )
    return similarity >= 0.6 or (bool(shared_markers) and similarity >= 0.25)


def has_new_market_detail(current: dict[str, Any], previous: dict[str, Any]) -> bool:
    current_title = str(current.get("title_ko") or current.get("title") or "").casefold()
    previous_title = str(previous.get("title_ko") or previous.get("title") or "").casefold()
    current_numbers = set(re.findall(r"\d+(?:[.,]\d+)?%?", current_title))
    previous_numbers = set(re.findall(r"\d+(?:[.,]\d+)?%?", previous_title))
    if current_numbers - previous_numbers:
        return True
    decision_terms = {"결정", "동결", "인상", "인하", "확정", "실제"}
    return any(term in current_title and term not in previous_title for term in decision_terms)


def headline_key(article: dict[str, Any]) -> str:
    title = str(article.get("title_ko") or article.get("title") or "").casefold()
    return re.sub(r"(?:발표|결과|시장|뉴스|속보|동향|마감)|[^0-9a-z가-힣]", "", title)


def headline_bigram_similarity(left: str, right: str) -> float:
    left_bigrams = {left[index : index + 2] for index in range(len(left) - 1)}
    right_bigrams = {right[index : index + 2] for index in range(len(right) - 1)}
    if not left_bigrams or not right_bigrams:
        return 0.0
    return len(left_bigrams & right_bigrams) / len(left_bigrams | right_bigrams)
