# Investing RSS Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Investing.com 세계동향 뉴스 수집을 공식 `news.rss` 단일 피드 기반으로 제한한다.

**Architecture:** `crawler-worker`의 Investing 전용 수집기는 `https://kr.investing.com/rss/news.rss`만 `search_rss_news()`에 전달한다. Google News fallback과 추가 Investing RSS 카테고리 피드는 제거하고, 기존 RSS 공통 파서는 유지한다.

**Tech Stack:** Python, `feedparser`, `unittest`, Docker requirements

## Global Constraints

- `서버 띄우는 쪽`과 `스케줄러 쪽`은 수정하지 않음.
- Investing 뉴스는 공식 `news.rss` 단일 URL만 사용함.
- Reuters/AP/MarketWatch 등 다른 카테고리의 RSS 수집은 유지함.
- 커밋 메시지는 한글 `유형: 설명` 형식을 사용함.

### Task 1: Investing 수집기 단일 피드 전환

**Files:**
- Modify: `crawler-worker/app/crawlers/investing_news_rss.py`
- Test: `tests/crawler_worker/test_investing_news_rss.py`

**Interfaces:**
- Consumes: `app.crawlers.rss_news.search_rss_news`
- Produces: `search_investing_news_rss(limit: int) -> list[dict]`

- [x] **Step 1: Write the failing test**
  - `search_rss_news()`가 한 번만 호출되고, 피드 URL이 공식 `news.rss` 하나인지 검증한다.
  - Google fallback 결과를 더 이상 병합하지 않는지 검증한다.

- [x] **Step 2: Run the focused test and verify it fails**
  - Run: `PYTHONPATH=crawler-worker python3 -m unittest tests.crawler_worker.test_investing_news_rss -v`
  - Expected: 기존 구현이 direct/fallback 두 번 호출되어 실패함.

- [x] **Step 3: Write minimal implementation**
  - `INVESTING_FEED_URLS`를 `news.rss` 단일 항목으로 축소한다.
  - `INVESTING_FALLBACK_FEED_URLS`와 fallback 호출 및 Google URL import를 제거한다.

- [x] **Step 4: Run focused tests**
  - Run: `PYTHONPATH=crawler-worker python3 -m unittest tests.crawler_worker.test_investing_news_rss tests.crawler_worker.test_rss_news -v`
  - Expected: all tests pass.

- [x] **Step 5: Commit**
  - Run: `git add crawler-worker/app/crawlers/investing_news_rss.py tests/crawler_worker/test_investing_news_rss.py && git commit -m "fix: 인베스팅 세계동향 RSS 단일화"`

### Task 2: 회귀 검증

**Files:**
- Verify: `crawler-worker/app/crawlers/investing_news_rss.py`
- Verify: `tests/crawler_worker/test_investing_news_rss.py`

- [x] **Step 1: Run Investing and related crawler tests**
  - Run: `PYTHONPATH=crawler-worker python3 -m unittest tests.crawler_worker.test_datetime_format tests.crawler_worker.test_investing_news_rss tests.crawler_worker.test_news_service tests.crawler_worker.test_rss_news -v`
  - Expected: all tests pass with zero failures.

- [x] **Step 2: Inspect the final diff and working tree**
  - Run: `git diff HEAD~1 --stat && git status --short --branch`
  - Expected: only the Investing RSS implementation and its tests are committed; no server or scheduler files changed.
