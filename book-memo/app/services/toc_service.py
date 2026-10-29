import html
import json
import re
from typing import Any

import requests


from app.services.yes24 import api_key, response_data, product_url

YES24_CONTENT_URL = "https://apis.yes24.com/v1/goods/content"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _content_isbn13(value: str) -> str:
    for token in value.split():
        if re.fullmatch(r"[0-9]{13}", token):
            return token
    for token in value.upper().split():
        if not re.fullmatch(r"[0-9]{9}[0-9X]", token):
            continue
        digits = [int(char) if char != "X" else 10 for char in token]
        if sum((10 - index) * digit for index, digit in enumerate(digits)) % 11:
            continue
        prefix = "978" + token[:9]
        check = (-sum(int(char) * (1 if index % 2 == 0 else 3)
                      for index, char in enumerate(prefix))) % 10
        return prefix + str(check)
    return ""


def fetch_toc_candidates(book: dict[str, Any], limit: int = 120) -> dict[str, Any]:
    key = api_key()
    if not key:
        raise ValueError("목차 조회를 사용하려면 YES24 API 키를 설정해주세요.")
    isbn = _content_isbn13(str(book.get("isbn") or "").strip())
    item_id = str(book.get("external_id") or "").strip()
    if book.get("source") == "yes24" and item_id.isdigit():
        params = {"searchType": "ItemId", "query": item_id}
    elif re.fullmatch(r"[0-9]{13}", isbn):
        params = {"searchType": "ISBN13", "query": isbn}
    else:
        return {"source": "", "candidates": []}
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.get(YES24_CONTENT_URL, params=params, headers={"X-Api-Key": key},
                             timeout=(3, 8), allow_redirects=False, stream=True) as response:
                chunks = []
                size = 0
                for chunk in response.iter_content(16384):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ValueError("YES24 응답이 너무 큽니다.")
                    chunks.append(chunk)
                payload = json.loads(b"".join(chunks))
                if response.status_code == 404 and isinstance(payload, dict) and payload.get("errorCode") in {"GOODS_001", "GOODS_002"}:
                    return {"source": "", "candidates": []}
                if response.status_code != 200:
                    raise ValueError("YES24 목차 조회 실패")
                data = response_data(payload).get("data")
                if not isinstance(data, dict) or not isinstance(data.get("contents"), str):
                    raise ValueError("YES24 목차 응답 형식 오류")
                candidates = _normalize_toc_lines(data["contents"], limit)
                source_url = product_url(data.get("itemId")) if candidates else ""
    except (requests.RequestException, ValueError, TypeError):
        raise ValueError("YES24 목차를 불러올 수 없습니다. API 키와 이용 상태를 확인해주세요.") from None
    return {"source": "YES24" if candidates else "", "candidates": candidates, "source_url": source_url}


def _normalize_toc_lines(toc_html: str, limit: int) -> list[str]:
    text = html.unescape(toc_html)
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</\s*p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\r", "\n")

    candidates = []
    seen = set()

    for raw_line in text.split("\n"):
        line = _clean_toc_line(raw_line)

        if not _is_useful_toc_line(line) or line in seen:
            continue

        candidates.append(line)
        seen.add(line)

        if len(candidates) >= limit:
            break

    return candidates


def _clean_toc_line(line: str) -> str:
    line = re.sub(r"^\s*[-*•·]\s*", "", line)
    line = re.sub(r"\s+", " ", line)
    return line.strip()


def _is_useful_toc_line(line: str) -> bool:
    if len(line) < 2 or len(line) > 120:
        return False

    if re.fullmatch(r"[\d\s.,~\-–—]+", line):
        return False

    if line in {"목차", "차례", "책소개", "저자 소개", "출판사 리뷰"}:
        return False

    return True
