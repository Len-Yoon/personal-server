# 뉴스 화면 자동 갱신 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 뉴스 화면이 열린 동안 60초마다 현재 카테고리 API를 확인하고 새 기사만 화면에 반영함.

**Architecture:** 기존 `/api/category` 응답과 5분 서버 캐시를 그대로 사용함. `search.html`에 작은 클라이언트 스크립트를 추가해 탭이 보이는 경우에만 폴링하고, 기사 URL 목록이 변경될 때 뉴스 목록과 상태 문구를 갱신함.

**Tech Stack:** FastAPI, Jinja2, vanilla JavaScript, unittest/pytest

## Global Constraints

- `서버 띄우는 쪽`과 `스케줄러 쪽`은 수정하지 않음.
- RSS 수집 주기는 기존 `NEWS_REFRESH_INTERVAL_SECONDS` 기본 300초를 유지함.
- API 오류 시 기존 뉴스 목록을 유지함.
- 전체 페이지 새로고침을 사용하지 않음.

---

### Task 1: 자동 갱신 화면 계약 테스트

**Files:**
- Modify: `/Users/len/PycharmProjects/personal-server/tests/crawler_worker/test_news_routes.py`
- Test: `/Users/len/PycharmProjects/personal-server/tests/crawler_worker/test_news_routes.py`

**Interfaces:**
- Consumes: `/category?category=KR_IT` HTML response
- Produces: HTML `data-auto-refresh-seconds`, `data-category`, `data-news-list` 속성 계약

- [ ] **Step 1: Write the failing test**

```python
    def test_category_page_exposes_auto_refresh_contract(self):
        app = self.load_app()

        with patch("app.routers.news.collect_korean_news") as mocked_collect:
            mocked_collect.return_value = {
                "category": "KR_IT",
                "label": "IT 동향",
                "description": "설명",
                "count": 0,
                "articles": [],
                "cache": {"hit": True, "age_seconds": 12, "ttl_seconds": 300},
            }

            from fastapi.testclient import TestClient
            with TestClient(app) as client:
                response = client.get("/category?category=KR_IT")

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-auto-refresh-seconds="60"', response.text)
        self.assertIn('data-category="KR_IT"', response.text)
        self.assertIn('data-news-list', response.text)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/crawler_worker/test_news_routes.py::CrawlerWorkerNewsRouteTests::test_category_page_exposes_auto_refresh_contract -q`

Expected: FAIL because the template does not expose the automatic refresh attributes.

- [ ] **Step 3: Implement the minimum template contract**

Add the three data attributes to the existing category page wrapper and news list without changing the route or collector.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/crawler_worker/test_news_routes.py::CrawlerWorkerNewsRouteTests::test_category_page_exposes_auto_refresh_contract -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/crawler_worker/test_news_routes.py crawler-worker/app/templates/search.html
git commit -m "test: 뉴스 자동 갱신 화면 계약 추가"
```

### Task 2: API 폴링과 조건부 DOM 갱신

**Files:**
- Modify: `/Users/len/PycharmProjects/personal-server/crawler-worker/app/templates/search.html`
- Modify: `/Users/len/PycharmProjects/personal-server/crawler-worker/app/static/css/style.css` only if status styling is required
- Test: `/Users/len/PycharmProjects/personal-server/tests/crawler_worker/test_news_routes.py`

**Interfaces:**
- Consumes: `data-category`, `data-auto-refresh-seconds`, `/api/category` JSON response
- Produces: 60초 폴링, 기사 URL 목록 비교, 변경 시 뉴스 카드 재구성, 오류 시 기존 DOM 유지

- [ ] **Step 1: Write the failing behavior test**

Extend the HTML contract test to assert that the page includes the refresh script marker and the refresh status target.

```python
        self.assertIn("news-auto-refresh", response.text)
        self.assertIn('data-refresh-status', response.text)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/crawler_worker/test_news_routes.py::CrawlerWorkerNewsRouteTests::test_category_page_exposes_auto_refresh_contract -q`

Expected: FAIL because the refresh script marker and status target do not exist.

- [ ] **Step 3: Implement the minimal client behavior**

Add an inline script that:

1. Reads the current category and interval from the page.
2. Skips polling when `document.hidden` is true.
3. Requests `/api/category?category=<category>` with a cache-busting query parameter.
4. Compares the response article URLs with the current article URLs.
5. Rebuilds only the news list when changed, preserving the existing card content and safe text insertion.
6. Updates count/cache status and keeps the current DOM unchanged on failure.
7. Calls `setInterval` using the page-provided 60-second interval.

- [ ] **Step 4: Run focused and route tests**

Run: `python3 -m pytest tests/crawler_worker/test_news_routes.py -q`

Expected: PASS.

- [ ] **Step 5: Run the crawler-worker test set**

Run: `python3 -m pytest tests/crawler_worker -q`

Expected: PASS with no failures.

- [ ] **Step 6: Commit**

```bash
git add crawler-worker/app/templates/search.html crawler-worker/app/static/css/style.css tests/crawler_worker/test_news_routes.py
git commit -m "feat: 뉴스 화면 자동 갱신 추가"
```

### Task 3: Final verification

**Files:**
- Verify: `/Users/len/PycharmProjects/personal-server/crawler-worker/app/routers/news.py`
- Verify: `/Users/len/PycharmProjects/personal-server/crawler-worker/app/services/news_archive.py`

- [ ] **Step 1: Confirm restricted areas are unchanged**

Run: `git diff --name-only HEAD~2..HEAD`

Expected: only the approved template, test, optional CSS, and documentation files are listed; no scheduler or server-start files are changed.

- [ ] **Step 2: Run the full test suite**

Run: `python3 -m pytest -q`

Expected: PASS with no failures. If optional environment-dependent tests are skipped, report the skip count.
