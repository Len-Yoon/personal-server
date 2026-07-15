# 뉴스 주제 버튼 확장 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 뉴스 페이지의 버튼 전환 방식에 IT 동향, AI 뉴스, 인기 스택 주제를 추가함.

**Architecture:** `crawler-worker`의 카테고리 정의와 Google News RSS 쿼리를 확장하고, 기존 `/category` 페이지의 버튼 목록에 새 주제를 노출함. 인베스팅 전용 흐름은 유지하되, 같은 화면에서 주제 버튼만 바꿔가며 다른 뉴스 목록을 보도록 구성함.

**Tech Stack:** Python, FastAPI, Jinja2, Google News RSS, unittest, pytest

## Global Constraints

- `서버 띄우는 쪽`과 `스케줄러 쪽`은 절대 수정하지 않음.
- 기능 변경이 필요하더라도 위 두 영역은 제외하고 작업함.
- 원본에 없는 내용을 확정 사실처럼 작성하지 않음.
- 확인되지 않은 내용은 반드시 `확인 필요`로 표시함.
- 업무 문서에 부적절한 말투를 사용하지 않음.

---

### Task 1: Google News RSS 주제 확장

**Files:**
- Modify: `/Users/len/PycharmProjects/personal-server/crawler-worker/app/crawlers/google_news_rss.py`
- Test: `/Users/len/PycharmProjects/personal-server/tests/crawler_worker/test_rss_news.py`

**Interfaces:**
- Consumes: `search_google_news_rss(category, limit, source_filter)`
- Produces: IT/AI/STACK 카테고리용 RSS 검색 쿼리

- [ ] **Step 1: Add failing test coverage for new RSS categories**
- [ ] **Step 2: Run the focused test to confirm the categories are missing**
- [ ] **Step 3: Add the new Google News query mappings**
- [ ] **Step 4: Run the focused test again to confirm it passes**
- [ ] **Step 5: Commit the focused change**

### Task 2: News category map and button list expansion

**Files:**
- Modify: `/Users/len/PycharmProjects/personal-server/crawler-worker/app/services/news_archive.py`
- Test: `/Users/len/PycharmProjects/personal-server/tests/crawler_worker/test_news_service.py`

**Interfaces:**
- Consumes: `_category_map()`, `get_categories()`, `collect_market_news()`
- Produces: new category codes and labels for the UI

- [ ] **Step 1: Add failing test coverage for expanded category list**
- [ ] **Step 2: Run the focused test to confirm only INVESTING exists**
- [ ] **Step 3: Extend category map and returned category list**
- [ ] **Step 4: Run the focused test again to confirm it passes**
- [ ] **Step 5: Commit the focused change**

### Task 3: News page button UI and routing verification

**Files:**
- Modify: `/Users/len/PycharmProjects/personal-server/crawler-worker/app/templates/search.html`
- Modify: `/Users/len/PycharmProjects/personal-server/crawler-worker/app/routers/news.py`

**Interfaces:**
- Consumes: `result.category`, `categories`, `/category?category=...`
- Produces: visible topic buttons that switch the news list

- [ ] **Step 1: Update the template if needed so the new categories render cleanly**
- [ ] **Step 2: Verify the category route still defaults to INVESTING**
- [ ] **Step 3: Run the relevant tests**
- [ ] **Step 4: Commit the UI wiring change**

### Task 4: End-to-end verification

**Files:**
- Test: `/Users/len/PycharmProjects/personal-server/tests/crawler_worker/test_news_service.py`
- Test: `/Users/len/PycharmProjects/personal-server/tests/crawler_worker/test_rss_news.py`

**Interfaces:**
- Consumes: the expanded category definitions and UI wiring
- Produces: passing regression coverage for the new topic buttons

- [ ] **Step 1: Run the targeted crawler-worker tests**
- [ ] **Step 2: Run the relevant broader test subset**
- [ ] **Step 3: Confirm there are no regressions in existing category behavior**
- [ ] **Step 4: Commit the final verification notes if needed**

