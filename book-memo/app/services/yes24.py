"""Shared YES24 authentication and application-error validation."""
import os


def api_key() -> str:
    return os.getenv("YES24_API_KEY", "").strip()


def product_url(item_id: object) -> str:
    value = str(item_id or "")
    if not value.isascii() or not value.isdigit():
        raise ValueError("YES24 상품 번호 형식 오류")
    return f"https://www.yes24.com/product/goods/{value}"


def response_data(payload: object) -> dict:
    # Never expose provider messages, response bodies or credentials to users.
    if not isinstance(payload, dict) or payload.get("success") is not True or payload.get("errorCode") is not None:
        raise ValueError("YES24 API 요청에 실패했습니다. API 키와 이용 한도를 확인해주세요.")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("YES24 응답 형식이 올바르지 않습니다.")
    return data
