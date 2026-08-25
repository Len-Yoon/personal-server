# 사용자 표시 시간 KST 통일 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 사용자 화면의 모든 시간 표시를 KST 기준 `YYYY-MM-DD HH:MM`으로 통일하고 `KST` 글자를 제거한다.

**Architecture:** 저장·정렬·쿨다운 판단은 UTC 원문을 유지한다. 각 서비스의 템플릿 직전 포매터만 `Asia/Seoul`로 변환하며, SQLite `CURRENT_TIMESTAMP`처럼 시간대가 없는 기존 값은 UTC로 간주한다.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, `zoneinfo`, unittest

**Spec:** `docs/superpowers/specs/2026-08-25-kst-display-unification-design.md`

## Global Constraints

- 내부 저장·비교용 시각은 UTC의 시간대 인식 ISO 8601 값을 유지한다.
- 사용자 화면, 알림, 날짜 기반 판단은 `Asia/Seoul` 기준을 사용한다.
- 사용자에게 노출하는 시각은 `YYYY-MM-DD HH:MM`만 표시하며 `KST`, 요일, 초, UTC 원문을 노출하지 않는다.
- 서버 기동 코드와 스케줄러 코드는 수정하지 않는다.

---

### Task 1: 포털 관리자 시간 표시 통일

**Files:**

- Modify: `portal-web/app/services/admin_status.py`
- Modify: `portal-web/app/services/security.py`
- Modify: `tests/test_portal_dashboard.py`
- Modify: `tests/test_portal_security.py`

**Interfaces:**

- Consumes: UTC ISO 8601 또는 기존 KST 문자열 시간값
- Produces: `format_status_checked_at(value: str) -> str`, `format_operation_history_for_display(history: list[dict[str, Any]]) -> list[dict[str, Any]]`

- [ ] **Step 1: 실패 테스트를 작성한다.** `format_status_checked_at("2026-07-09T01:02:03+00:00")`의 기대값을 `"2026-07-09 10:02"`로 바꾸고, `append_security_event()`로 만든 최근 이벤트가 `^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$` 형식인지 검증한다.

- [ ] **Step 2: 실패를 확인한다.**

Run: `PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard.PortalDashboardTests.test_admin_status_checked_at_is_formatted_for_display tests.test_portal_security.PortalSecurityTests.test_security_event_timestamp_uses_compact_kst_display`

Expected: 기존 `KST` 또는 초 단위 표기 때문에 실패.

- [ ] **Step 3: 최소 구현을 작성한다.** `format_status_checked_at()`은 `parsed.astimezone(SEOUL_TIMEZONE).strftime("%Y-%m-%d %H:%M")`를 반환하게 하고, `append_security_event()`은 `now.strftime("%Y-%m-%d %H:%M")`을 기록한다.

- [ ] **Step 4: 포털 테스트를 통과시킨다.**

Run: `python3 tests/run_service_tests.py --suite portal`

Expected: PASS.

- [ ] **Step 5: 커밋한다.**

Run: `git add portal-web/app/services/admin_status.py portal-web/app/services/security.py tests/test_portal_dashboard.py tests/test_portal_security.py && git commit -m "fix: 포털 시간 표시 형식 통일"`

### Task 2: 뉴스 시간 표시 형식 통일

**Files:**

- Modify: `crawler-worker/app/services/datetime_format.py`
- Modify: `crawler-worker/app/templates/search.html`
- Modify: `tests/crawler_worker/test_datetime_format.py`

**Interfaces:**

- Consumes: ISO 8601 또는 RFC 822 기사 시각
- Produces: `format_news_datetime(value: str) -> str`

- [ ] **Step 1: 실패 테스트를 작성한다.** `format_news_datetime("2026-07-10T15:58:15.761236+00:00")`의 기대값을 `"2026-07-11 00:58"`로 바꾼다.

- [ ] **Step 2: 실패를 확인한다.**

Run: `PYTHONPATH=crawler-worker python3 -m unittest tests.crawler_worker.test_datetime_format.DateTimeFormatTests.test_formats_utc_iso_as_compact_kst_datetime`

Expected: 기존 한글 날짜·요일·초 형식 때문에 실패.

- [ ] **Step 3: 최소 구현을 작성한다.** `local = parsed.astimezone(KOREA_TIMEZONE)` 뒤 `return local.strftime("%Y-%m-%d %H:%M")`으로 반환값을 통일한다. 자동 새로고침을 담당하는 `search.html`의 브라우저 포매터도 같은 형식을 표시한다.

- [ ] **Step 4: 뉴스 테스트를 통과시킨다.**

Run: `python3 tests/run_service_tests.py --suite crawler-worker`

Expected: PASS.

- [ ] **Step 5: 커밋한다.**

Run: `git add crawler-worker/app/services/datetime_format.py crawler-worker/app/templates/search.html tests/crawler_worker/test_datetime_format.py && git commit -m "fix: 뉴스 시간 표시 형식 통일"`

### Task 3: 책 메모와 YouTube 메모 시간 표시 통일

**Files:**

- Create: `book-memo/app/services/datetime_format.py`
- Modify: `book-memo/app/main.py`
- Modify: `book-memo/app/templates/book_detail.html`
- Modify: `tests/book_memo/test_ui_contract.py`
- Create: `youtube-memo/app/services/datetime_format.py`
- Modify: `youtube-memo/app/main.py`
- Modify: `youtube-memo/app/templates/video_detail.html`
- Modify: `tests/youtube_memo/test_ui_contract.py`

**Interfaces:**

- Consumes: SQLite UTC 문자열 `YYYY-MM-DD HH:MM:SS` 또는 시간대 인식 ISO 문자열
- Produces: Jinja filter `display_datetime(value: str) -> str`

- [ ] **Step 1: 실패 테스트를 작성한다.** 책·YouTube 상세 화면에 UTC 메모를 만든 뒤 HTML에 `YYYY-MM-DD HH:MM` KST 변환값이 있고 원문 UTC가 없는지 각각 검증한다.

- [ ] **Step 2: 실패를 확인한다.**

Run: `python3 -m unittest tests.book_memo.test_ui_contract tests.youtube_memo.test_ui_contract`

Expected: 현재 템플릿이 원문 `memo.created_at`을 출력하므로 새 테스트가 실패.

- [ ] **Step 3: 표시 전용 포매터와 필터를 구현한다.** 두 서비스에 `format_display_datetime(value: str) -> str`을 만들고, `datetime.fromisoformat()` 결과가 naive이면 `timezone.utc`를 설정한 뒤 `ZoneInfo("Asia/Seoul")`으로 변환하여 `strftime("%Y-%m-%d %H:%M")`을 반환한다. 각 `main.py`에서 `templates.env.filters["display_datetime"]`를 등록하고 템플릿의 `memo.created_at`을 `memo.created_at|display_datetime`으로 교체한다.

- [ ] **Step 4: 메모 서비스 테스트를 통과시킨다.**

Run: `python3 tests/run_service_tests.py --suite book-memo && python3 tests/run_service_tests.py --suite youtube-memo`

Expected: PASS.

- [ ] **Step 5: 커밋한다.**

Run: `git add book-memo/app youtube-memo/app tests/book_memo/test_ui_contract.py tests/youtube_memo/test_ui_contract.py && git commit -m "fix: 메모 시간 표시를 KST로 통일"`

### Task 4: 통합 검증과 규칙 확인

**Files:**

- Modify: `AGENTS.md` (표시 형식 `YYYY-MM-DD HH:MM` 명시)

**Interfaces:**

- Consumes: 각 서비스의 표시 포매터
- Produces: 통일된 표시 형식과 프로젝트 규칙

- [ ] **Step 1: 사용자 표시 시간 위치를 재검색한다.**

Run: `rg -n "KST|created_at|published_at|collected_at|captured_at|timestamp" portal-web/app/templates crawler-worker/app/templates book-memo/app/templates youtube-memo/app/templates`

Expected: 사용자 표시 문자열에 `KST`와 원문 시간 출력이 없음.

- [ ] **Step 2: 전체 서비스 테스트를 실행한다.**

Run: `python3 tests/run_service_tests.py`

Expected: PASS. 로컬 콜백 포트 제약이 있으면 car-care-worker 스위트만 권한 환경에서 재실행.

- [ ] **Step 3: 변경 범위와 공백을 검토한다.**

Run: `git diff --check && git diff --stat origin/main...HEAD`

Expected: 공백 오류 없음, 서버 기동·스케줄러 파일 변경 없음.

- [ ] **Step 4: 커밋한다.**

Run: `git add AGENTS.md && git commit -m "docs: 시간 표시 형식 기준 명확화"`
