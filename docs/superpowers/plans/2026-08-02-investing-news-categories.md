# Investing.com 카테고리 뉴스 수집 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Investing.com 뉴스 수집을 상품과 선물 및 주식 시장 RSS로 제한함.

**Architecture:** RSS URL 목록만 교체하고 기존 정제·중복 제거·보관 흐름은 유지함. 이전 캐시 파일은 삭제하여 기존 전체 뉴스가 다시 노출되지 않게 함.

**Tech Stack:** Python, unittest, Investing.com RSS

## Global Constraints

- 수집 대상은 `news_11.rss`와 `news_25.rss`만 사용함.
- 기존 뉴스 보관 캐시를 삭제함.

---

### Task 1: 카테고리 RSS 수집 제한

**Files:**
- Modify: `crawler-worker/app/crawlers/investing_news_rss.py:12-14`
- Test: `tests/crawler_worker/test_investing_news_rss.py:75-81`

**Interfaces:**
- Produces: `search_investing_news_rss(limit: int) -> list[dict]`가 지정된 두 RSS에서 기사 수집함.

- [ ] **Step 1: 실패 테스트 작성**

```python
self.assertEqual(
    direct_call["feed_urls"],
    [
        "https://kr.investing.com/rss/news_11.rss",
        "https://kr.investing.com/rss/news_25.rss",
    ],
)
```

- [ ] **Step 2: 실패 확인**

Run: `python3 -m unittest tests.crawler_worker.test_investing_news_rss -v`
Expected: 기존 `news.rss` 값으로 인해 실패함.

- [ ] **Step 3: 최소 구현**

```python
INVESTING_FEED_URLS = [
    "https://kr.investing.com/rss/news_11.rss",
    "https://kr.investing.com/rss/news_25.rss",
]
```

- [ ] **Step 4: 통과 확인**

Run: `python3 -m unittest tests.crawler_worker.test_investing_news_rss -v`
Expected: PASS

### Task 2: 기존 보관 뉴스 삭제

**Files:**
- Delete: `data/crawler-worker/news_archive.json` (Git 미추적 캐시 파일)

- [ ] **Step 1: 파일 삭제**

Run: `rm data/crawler-worker/news_archive.json`
Expected: 다음 수집 전까지 기존 보관 뉴스가 없음.
