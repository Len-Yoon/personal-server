# 뉴스 레거시 경로 및 환경변수 정리 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 현재 공개 뉴스 기능만 남기고 사용되지 않는 글로벌 다중 소스 경로와 OpenAI 환경변수 예시를 제거함.

**Architecture:** 한국어 뉴스 수집은 Investing.com과 Google News만 사용하도록 `news_sources`와 `news_archive`를 축소함. 글로벌 수집기·전용 테스트는 삭제하고, 한국어 공개 기능과 알림 계약을 테스트로 유지함.

**Tech Stack:** Python, FastAPI, RSS, unittest, Docker Compose

## Global Constraints

- 서버 기동·스케줄러·Compose 구성은 수정하지 않음.
- `KR_WORLD`, `KR_IT`, `KR_AI` 공개 화면과 Telegram 알림 정책을 유지함.
- Caddy는 현재 배포·자동기동 대상이므로 변경하지 않음.

---

### Task 1: 글로벌 뉴스 레거시 제거

**Files:**
- Modify: `crawler-worker/app/services/news_archive.py`, `crawler-worker/app/services/news_sources.py`, `tests/crawler_worker/test_news_service.py`
- Delete: `crawler-worker/app/crawlers/ap_news_rss.py`, `crawler-worker/app/crawlers/marketwatch_news_rss.py`, `crawler-worker/app/crawlers/reuters_news_rss.py`
- Test: `tests/crawler_worker/test_news_service.py`, `tests/crawler_worker/test_news_routes.py`, `tests/crawler_worker/test_news_scheduler.py`

- [x] **Step 1: 글로벌 API 부재 계약 테스트를 작성함**
- [x] **Step 2: 테스트가 기존 글로벌 API 때문에 실패하는지 확인함**
- [x] **Step 3: 글로벌 수집·카테고리 코드와 전용 수집기를 제거함**
- [x] **Step 4: 한국어 뉴스·알림 회귀 테스트를 실행함**

### Task 2: 사용하지 않는 OpenAI 환경변수 정리

**Files:**
- Modify: `.env.example`, `docs/operations-reference.md`
- Test: 환경변수 키 검색, `git diff --check`

- [x] **Step 1: `.env.example`에 OpenAI 예비 항목이 존재하는지 확인함**
- [x] **Step 2: 사용하지 않는 예비 항목과 운영 문서 설명을 제거함**
- [x] **Step 3: 코드 참조와 문서 형식을 검증함**
