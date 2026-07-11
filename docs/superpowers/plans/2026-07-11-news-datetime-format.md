# 뉴스 날짜·시간 표시 형식 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 프로젝트 뉴스 화면의 모든 날짜·시간을 한국시간 `2026년 7월 10일 금요일 10:10:10` 형식으로 표시한다.

**Architecture:** 원본 시간값은 수집·정렬에 그대로 보존하고, `crawler-worker`의 Jinja 공통 필터에서 화면 출력 직전에 Asia/Seoul 변환과 한국어 포맷팅을 수행한다. 파싱 실패 시 원문을 반환한다.

**Tech Stack:** Python `datetime`, FastAPI/Jinja2, pytest

## Global Constraints

- 화면 표시 시간대는 `Asia/Seoul`이다.
- 출력 형식은 `YYYY년 M월 D일 요일 HH:MM:SS`이다.
- 수집·정렬·캐시용 원본 시간 필드는 변경하지 않는다.

### Task 1: 공통 한국시간 포맷터

**Files:**
- Create: `crawler-worker/app/services/datetime_format.py`
- Test: `tests/crawler_worker/test_datetime_format.py`

- [ ] **Step 1: Write the failing test**

```python
def test_formats_utc_iso_as_korean_datetime():
    assert format_news_datetime("2026-07-10T15:58:15.761236+00:00") == "2026년 7월 11일 토요일 00:58:15"

def test_formats_rfc822_as_korean_datetime():
    assert format_news_datetime("Fri, 10 Jul 2026 15:45:00 GMT") == "2026년 7월 11일 토요일 00:45:00"

def test_returns_original_value_when_unparseable():
    assert format_news_datetime("not-a-date") == "not-a-date"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/crawler_worker/test_datetime_format.py -q`
Expected: FAIL because `datetime_format` does not exist.

- [ ] **Step 3: Write minimal implementation**

Implement `format_news_datetime(value: str) -> str` with `datetime.fromisoformat` first, `email.utils.parsedate_to_datetime` fallback, UTC for naive values, `ZoneInfo("Asia/Seoul")`, and Korean weekday names.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/crawler_worker/test_datetime_format.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add crawler-worker/app/services/datetime_format.py tests/crawler_worker/test_datetime_format.py docs/superpowers/specs/2026-07-11-news-datetime-format-design.md docs/superpowers/plans/2026-07-11-news-datetime-format.md
git commit -m "feat: 뉴스 한국시간 표시 포맷 추가"
```

### Task 2: 뉴스 템플릿 전체 적용

**Files:**
- Modify: `crawler-worker/app/routers/news.py`
- Modify: `crawler-worker/app/templates/search.html`
- Modify: `crawler-worker/app/templates/saved.html`
- Test: `tests/crawler_worker/test_datetime_format.py`

- [ ] **Step 1: Write the failing template integration test**

Assert that the Jinja environment exposes `format_news_datetime` and that both templates use the `|news_datetime` filter for `collected_at` and `published_at`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/crawler_worker/test_datetime_format.py -q`
Expected: FAIL because the filter is not registered and templates still render raw values.

- [ ] **Step 3: Write minimal implementation**

Register the filter once on the shared Jinja environment in `news.py` and change every visible `collected_at`/`published_at` expression in `search.html` and `saved.html` to `|news_datetime`.

- [ ] **Step 4: Run focused and regression tests**

Run: `python -m pytest tests/crawler_worker/test_datetime_format.py tests/crawler_worker/test_news_service.py -q`
Expected: all focused tests pass.

- [ ] **Step 5: Commit**

```bash
git add crawler-worker/app/routers/news.py crawler-worker/app/templates/search.html crawler-worker/app/templates/saved.html tests/crawler_worker/test_datetime_format.py
git commit -m "feat: 뉴스 화면 시간 표시를 한국시간으로 통일"
```
