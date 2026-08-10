# 기존 UI 복귀 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 화면 디자인을 복원하면서 나스닥 알림, 파일 탐색기, 관리자 수집 시각의 기능 개선을 유지함.

**Architecture:** 시각 전용 Atlas 템플릿·CSS 변경은 기준 브랜치의 화면으로 되돌림. 기능 계약이 필요한 파일함, 뉴스 상태, 관리자 수집 시각은 기존 디자인 구조 안에서 필요한 데이터·조작 요소만 유지하도록 최소 재적용함.

**Tech Stack:** Python, FastAPI, Jinja2, CSS, browser JavaScript, unittest, Node test runner.

## Global Constraints

- 서버 기동 영역과 스케줄러 구현은 수정하지 않음.
- 현재 15분 수집 주기와 Telegram 일일 건수 무제한 정책을 유지함.
- 기존 인증·업로드·다운로드·삭제 보안 계약을 변경하지 않음.
- Atlas 전용 색상, 카드, 레이아웃, 공통 base 템플릿 변경은 남기지 않음.
- 기능에 필요한 접근성 속성, 검색·정렬·보기 전환 제어는 기존 UI에서 사용 가능한 최소 마크업으로 유지함.

---

### Task 1: 시각 전용 화면을 기준 UI로 복원함

**Files:**
- Modify: `portal-web/app/templates/base.html`, `portal-web/app/templates/dashboard.html`, `portal-web/app/templates/admin_status.html`, `portal-web/app/static/css/style.css`
- Modify: `crawler-worker/app/templates/base.html`, `crawler-worker/app/templates/search.html`, `crawler-worker/app/templates/saved.html`, `crawler-worker/app/static/css/style.css`
- Modify: `youtube-memo/app/templates/home.html`, `youtube-memo/app/templates/video_detail.html`, `youtube-memo/app/static/css/style.css`
- Modify: `book-memo/app/templates/home.html`, `book-memo/app/templates/book_detail.html`, `book-memo/app/static/css/style.css`
- Test: `tests/test_portal_dashboard.py`, `tests/crawler_worker/test_news_routes.py`, `tests/youtube_memo/test_ui_contract.py`, `tests/book_memo/test_ui_contract.py`

**Interfaces:**
- Consumes: 기존 템플릿 컨텍스트와 라우트 URL.
- Produces: 기준 브랜치와 같은 화면 구조·공통 스타일, 기존 기능 URL과 폼 계약 유지.

- [ ] **Step 1: 기존 UI 계약 테스트를 조정함**

Atlas 전용 클래스가 아닌 기존 페이지 제목, 서비스 링크, 폼 action, 뉴스 기사 링크가 유지되는지 검증함.

- [ ] **Step 2: 테스트가 현재 Atlas 화면에서 실패하는지 확인함**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard tests.crawler_worker.test_news_routes tests.youtube_memo.test_ui_contract tests.book_memo.test_ui_contract -v`

Expected: 기존 UI 구조 검증이 Atlas 전용 마크업에서 실패함.

- [ ] **Step 3: 기준 브랜치의 시각 전용 파일을 복원함**

`git show a35beb8:<path>`를 기준으로 템플릿과 CSS를 복원하고, 기본 CSS 캐시 버전은 변경된 파일을 즉시 받도록 새 값으로 설정함.

- [ ] **Step 4: 집중 UI 계약 테스트를 통과시킴**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard tests.crawler_worker.test_news_routes tests.youtube_memo.test_ui_contract tests.book_memo.test_ui_contract -v`

Expected: PASS.

### Task 2: 기존 디자인 안에 기능 개선을 최소 재적용함

**Files:**
- Modify: `portal-web/app/templates/files.html`, `portal-web/app/static/css/style.css`
- Modify: `portal-web/app/templates/admin_status.html`
- Modify: `crawler-worker/app/templates/search.html`, `crawler-worker/app/templates/saved.html`
- Modify: `tests/test_file_access.py`, `tests/test_portal_dashboard.py`, `tests/crawler_worker/test_news_routes.py`, `tests/file_explorer_client.test.mjs`

**Interfaces:**
- Consumes: `status_checked_at`, 기사 `nasdaq_relevance`, 파일 목록과 기존 인증 라우트.
- Produces: 기존 UI 안의 실제 호스트 수집 시각, alert/archive 표기, 검색·이름/수정일 정렬·목록/아이콘 보기·키보드 선택.

- [ ] **Step 1: 기능 회귀 테스트를 기존 UI 기준으로 작성함**

파일함 테스트는 `role="toolbar"`, 검색 입력, 보기 전환, 이름·수정일 정렬을 확인함. 뉴스 테스트는 alert/archive 이유 텍스트를 확인함. 관리자 테스트는 호스트 수집 시각과 unknown 대체를 확인함.

- [ ] **Step 2: 복원 직후 기능 테스트가 실패하는지 확인함**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=portal-web python3 -m unittest tests.test_file_access tests.test_portal_dashboard -v && cd crawler-worker && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:.. python3 -m unittest ../tests/crawler_worker/test_news_routes.py -v && cd .. && node --test tests/file_explorer_client.test.mjs`

Expected: 복원으로 제거된 기능 마크업·동작 계약에서 FAIL.

- [ ] **Step 3: 기존 클래스 체계만 사용해 최소 기능 마크업과 스크립트를 복원함**

파일함에는 기존 파일 표 안의 명령 모음·검색·정렬·보기 제어만 추가함. 뉴스에는 각 Investing 기사 아래 alert/archive와 이유를 표시함. 관리자에는 `호스트 수집 시각` 라벨과 `<time>`만 추가함.

- [ ] **Step 4: 기능·보안 회귀 테스트를 통과시킴**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=portal-web python3 -m unittest tests.test_file_access tests.test_portal_dashboard tests.test_portal_security tests.test_portfolio -v && cd crawler-worker && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:.. python3 -m unittest discover -s ../tests/crawler_worker -v && cd .. && node --test tests/file_explorer_client.test.mjs`

Expected: PASS.

### Task 3: 전체 검증과 변경 범위 확인

**Files:**
- Test: `tests/book_memo/test_ui_contract.py`, `tests/book_memo/test_book_service.py`, `tests/youtube_memo/test_ui_contract.py`, `tests/youtube_memo/test_video_titles.py`

- [ ] **Step 1: 메모 서비스 회귀 테스트를 실행함**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.book_memo.test_ui_contract tests.book_memo.test_book_service tests.youtube_memo.test_ui_contract tests.youtube_memo.test_video_titles -v`

Expected: PASS.

- [ ] **Step 2: 변경 범위를 확인함**

Run: `git diff --check a35beb8..HEAD && git diff --name-only a35beb8..HEAD`

Expected: 형식 오류 없음, 서버 기동·스케줄러 파일 없음.

- [ ] **Step 3: 변경을 커밋함**

```bash
git add portal-web crawler-worker youtube-memo book-memo tests docs/superpowers/plans/2026-08-10-legacy-ui-restore.md
git commit -m "style: 기존 UI에 기능 개선 적용"
```
