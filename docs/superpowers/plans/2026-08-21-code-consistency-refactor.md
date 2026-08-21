# 코드 일관성 리팩터링 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 외부 기능과 운영 구성을 변경하지 않고 포털 라우팅과 뉴스 보관 코드의 책임 경계를 분리함. 메모 인증 중복은 Docker build context 제약으로 보류하고 기존 계약 테스트로 보호함.

**Architecture:** 서비스별 Docker build context를 유지하며 서비스 내부에서만 모듈을 분리함. 기존 라우트와 공개 서비스 함수는 facade 또는 router 등록으로 유지함.

**Tech Stack:** Python 3.11, FastAPI, SQLite, unittest, Node.js test runner

**Spec:** `docs/superpowers/specs/2026-08-21-code-consistency-refactor-design.md`

## Global Constraints

- 기존 URL, HTTP 상태 코드, 응답 JSON, 템플릿 컨텍스트, 환경 변수명, SQLite 스키마를 변경하지 않음.
- Compose, Dockerfile, 배포 workflow, deployment script, `news_scheduler.py`를 수정하지 않음.
- 서비스 간 루트 공통 패키지를 만들지 않음.
- 모든 작업은 `python3 tests/run_service_tests.py`와 `git diff --check`로 검증함.

---

### Task 1: 포털 공개·관리자 라우터 분리

**Files:**
- Create: `portal-web/app/routers/admin.py`
- Modify: `portal-web/app/routers/dashboard.py`
- Modify: `portal-web/app/main.py`
- Test: `tests/test_portal_dashboard.py`
- Test: `tests/test_portal_security.py`
- Test: `tests/test_homeops.py`

**Interfaces:**
- Produces: `dashboard.router`는 `/`, `/news`, `/memo`, `/books`만 제공함.
- Produces: `admin.router`는 `/admin/status`, `/admin/security`, `/admin/homeops/**`, `/internal/homeops/scan`을 제공함.
- Compatibility: `homeops_admin_session` cookie, security event, 응답 redirect를 유지함.

- [ ] **Step 1: 기존 포털·HomeOps 계약 테스트를 실행하여 현재 동작을 고정함**

Run: `python3 -m unittest tests.test_portal_dashboard tests.test_portal_security tests.test_homeops tests.test_homeops_notifier -v`

Expected: 관리자 인증·HomeOps 승인·cross-origin 거부가 통과함.

- [ ] **Step 2: 관리자와 HomeOps route·helper를 `admin.py`로 이동하고 main에 등록함**

```python
app.include_router(dashboard.router)
app.include_router(admin.router)
```

- [ ] **Step 3: 포털·HomeOps 회귀 테스트를 실행함**

Run: `python3 -m unittest tests.test_portal_dashboard tests.test_portal_security tests.test_homeops tests.test_homeops_notifier -v`

Expected: 관리자 인증·HomeOps 승인·cross-origin 거부가 통과함.

- [ ] **Step 4: 작업 파일을 명시적으로 stage하고 커밋함**

Run: `git add portal-web/app/main.py portal-web/app/routers/dashboard.py portal-web/app/routers/admin.py tests/test_portal_dashboard.py tests/test_portal_security.py tests/test_homeops.py && git commit -m "refactor: 포털 관리자 라우터 분리"`

### Task 2: 뉴스 보관 facade 내부 책임 분리

**Files:**
- Create: `crawler-worker/app/services/news_archive_storage.py`
- Create: `crawler-worker/app/services/news_archive_processing.py`
- Create: `crawler-worker/app/services/news_archive_notifications.py`
- Modify: `crawler-worker/app/services/news_archive.py`
- Test: `tests/crawler_worker/test_news_service.py`
- Test: `tests/crawler_worker/test_investing_news_rss.py`

**Interfaces:**
- Produces: facade의 `collect_korean_news`, `list_recent_news`, `get_korean_categories` 시그니처와 반환 데이터 유지.
- Storage는 archive JSON 경로·기본값·읽기·저장, processing은 market topic·이벤트 중복 판정, notifications는 Telegram 상태 입력 정규화를 담당함. 수집·만료 정리·검색·digest 전송 흐름은 facade에 유지함.

- [ ] **Step 1: 알림 전송 실패 시 pending 유지 테스트를 작성함**

```python
archive = {"telegram_pending_articles": [], "telegram_recent_articles": []}
notifications.queue_and_send_general_digest(archive, articles, now, sender=lambda _: False)
self.assertEqual(archive["telegram_pending_articles"], articles)
```

- [ ] **Step 2: notifications 모듈 부재로 테스트가 실패하는지 확인함**

Run: `PYTHONPATH=crawler-worker python3 -m unittest tests.crawler_worker.test_news_service -v`

Expected: `news_archive_notifications` import 부재로 실패함.

- [ ] **Step 3: 저장·처리·알림 보조 함수를 새 모듈로 이동하고 facade에서 위임함**

```python
def collect_korean_news(category: str = "KR_WORLD", limit: int = 24, force_refresh: bool = False) -> dict[str, Any]:
    return facade.collect_korean_news(category, limit, force_refresh)
```

- [ ] **Step 4: crawler-worker 회귀 테스트를 실행함**

Run: `python3 tests/run_service_tests.py --suite crawler-worker`

Expected: archive 보존·중복 제거·알림 정책·검색 API 테스트가 통과함.

- [ ] **Step 5: 작업 파일을 명시적으로 stage하고 커밋함**

Run: `git add crawler-worker/app/services/news_archive.py crawler-worker/app/services/news_archive_storage.py crawler-worker/app/services/news_archive_processing.py crawler-worker/app/services/news_archive_notifications.py tests/crawler_worker/test_news_service.py tests/crawler_worker/test_investing_news_rss.py && git commit -m "refactor: 뉴스 보관 서비스 책임 분리"`

### Task 3: 통합 검증과 PR

**Files:**
- Modify: `README.md` only if 실제 실행 명령이 바뀐 경우

- [ ] **Step 1: 전체 회귀 테스트를 실행함**

Run: `python3 tests/run_service_tests.py`

Expected: 모든 8개 suite가 PASS.

- [ ] **Step 2: 금지 영역과 공백 오류를 확인함**

Run: `git diff --check && git diff --name-only origin/main...HEAD`

Expected: Compose, Dockerfile, workflow, deployment script, scheduler 파일이 목록에 없음.

- [ ] **Step 3: PR CI·Agent Review 통과 후 병합함**

Run: `gh pr create --base main --head Len-Yoon:codex/full-consistency-refactor --title "refactor: 서비스 코드 책임 경계 정리"`

Expected: CI, Agent Review, main CI, N100 배포가 모두 성공함.

- [ ] **Step 4: 병합된 branch와 분리 작업공간을 자동 정리함**

Run: `git fetch --prune origin && git worktree remove .worktrees/full-consistency-refactor`

Expected: 병합·CI·배포 성공 및 작업공간 무변경 조건 후 정리됨.
