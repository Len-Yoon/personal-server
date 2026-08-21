# 코드 일관성 리팩터링 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 외부 기능과 운영 구성을 변경하지 않고 메모 인증, 포털 라우팅, 뉴스 보관 코드의 책임 경계를 분리함.

**Architecture:** 서비스별 Docker build context를 유지하며 서비스 내부에서만 모듈을 분리함. 기존 라우트와 공개 서비스 함수는 facade 또는 router 등록으로 유지함.

**Tech Stack:** Python 3.11, FastAPI, SQLite, unittest, Node.js test runner

**Spec:** `docs/superpowers/specs/2026-08-21-code-consistency-refactor-design.md`

## Global Constraints

- 기존 URL, HTTP 상태 코드, 응답 JSON, 템플릿 컨텍스트, 환경 변수명, SQLite 스키마를 변경하지 않음.
- Compose, Dockerfile, 배포 workflow, deployment script, `news_scheduler.py`를 수정하지 않음.
- 서비스 간 루트 공통 패키지를 만들지 않음.
- 모든 작업은 `python3 tests/run_service_tests.py`와 `git diff --check`로 검증함.

---

### Task 1: 메모 서비스 인증 책임 분리

**Files:**
- Create: `youtube-memo/app/services/write_auth.py`
- Create: `book-memo/app/services/write_auth.py`
- Modify: `youtube-memo/app/main.py`
- Modify: `book-memo/app/main.py`
- Test: `tests/youtube_memo/test_ui_contract.py`
- Test: `tests/book_memo/test_ui_contract.py`

**Interfaces:**
- Produces: `WriteAuth.create_session() -> str`, `has_session(token: str) -> bool`, `revoke_session(token: str) -> None`, `safe_redirect(path: str) -> str`.
- Compatibility: 기존 `main.py` helper와 cookie·origin·rate-limit 상태 파일 이름을 유지함.

- [ ] **Step 1: session revoke 계약 테스트를 작성함**

```python
token = main._create_write_session()
self.assertTrue(main._has_write_session_token(token))
main._revoke_write_session(token)
self.assertFalse(main._has_write_session_token(token))
```

- [ ] **Step 2: 새 테스트가 helper 부재로 실패하는지 확인함**

Run: `PYTHONPATH=youtube-memo python3 -m unittest tests.youtube_memo.test_ui_contract -v`

Expected: `_has_write_session_token` 부재로 실패함.

- [ ] **Step 3: 서비스별 `WriteAuth` 구현과 main 위임 함수를 추가함**

```python
class WriteAuth:
    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self.sessions[token] = datetime.now(timezone.utc) + timedelta(seconds=self.max_age_seconds)
        return token

    def has_session(self, token: str) -> bool:
        return bool(token and self.sessions.get(token, datetime.min.replace(tzinfo=timezone.utc)) > datetime.now(timezone.utc))

    def revoke_session(self, token: str) -> None:
        self.sessions.pop(token, None)

    def safe_redirect(self, path: str) -> str:
        return path if path.startswith("/") and not path.startswith("//") else ""
```

- [ ] **Step 4: 메모 서비스 계약 테스트를 실행함**

Run: `python3 tests/run_service_tests.py --suite youtube-memo && python3 tests/run_service_tests.py --suite book-memo`

Expected: 로그인·로그아웃·Origin 거부·프로세스 간 rate-limit 테스트가 통과함.

- [ ] **Step 5: 작업을 커밋함**

Run: `git commit -am "refactor: 메모 서비스 쓰기 인증 책임 분리"`

### Task 2: 포털 공개·관리자 라우터 분리

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

- [ ] **Step 1: admin router 등록 계약 테스트를 작성함**

```python
paths = {route.path for route in app.routes}
self.assertIn("/admin/status", paths)
self.assertIn("/internal/homeops/scan", paths)
```

- [ ] **Step 2: 등록 전 테스트가 실패하는지 확인함**

Run: `PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard -v`

Expected: 새 `admin.router` 등록 전에는 계약 테스트가 실패함.

- [ ] **Step 3: 관리자와 HomeOps route·helper를 `admin.py`로 이동하고 main에 등록함**

```python
app.include_router(dashboard.router)
app.include_router(admin.router)
```

- [ ] **Step 4: 포털·HomeOps 회귀 테스트를 실행함**

Run: `python3 -m unittest tests.test_portal_dashboard tests.test_portal_security tests.test_homeops tests.test_homeops_notifier -v`

Expected: 관리자 인증·HomeOps 승인·cross-origin 거부가 통과함.

- [ ] **Step 5: 작업을 커밋함**

Run: `git commit -am "refactor: 포털 관리자 라우터 분리"`

### Task 3: 뉴스 보관 facade 내부 책임 분리

**Files:**
- Create: `crawler-worker/app/services/news_archive_storage.py`
- Create: `crawler-worker/app/services/news_archive_processing.py`
- Create: `crawler-worker/app/services/news_archive_notifications.py`
- Modify: `crawler-worker/app/services/news_archive.py`
- Test: `tests/crawler_worker/test_news_service.py`
- Test: `tests/crawler_worker/test_investing_news_rss.py`

**Interfaces:**
- Produces: facade의 `collect_korean_news`, `list_recent_news`, `get_korean_categories` 시그니처와 반환 데이터 유지.
- Storage는 archive JSON 읽기·저장·만료 제거, processing은 정규화·검색·중복 제거, notifications는 Telegram 대기열·cooldown·전송 결과만 담당.

- [ ] **Step 1: 알림 전송 실패 시 pending 유지 테스트를 작성함**

```python
archive = {"telegram_pending_articles": [], "telegram_recent_articles": []}
notifications.queue_and_send_general_digest(archive, articles, now, sender=lambda _: False)
self.assertEqual(archive["telegram_pending_articles"], articles)
```

- [ ] **Step 2: notifications 모듈 부재로 테스트가 실패하는지 확인함**

Run: `PYTHONPATH=crawler-worker python3 -m unittest tests.crawler_worker.test_news_service -v`

Expected: `news_archive_notifications` import 부재로 실패함.

- [ ] **Step 3: 저장·처리·알림 함수를 새 모듈로 이동하고 facade에서 위임함**

```python
def collect_korean_news(category: str = "KR_WORLD", limit: int = 24, force_refresh: bool = False) -> dict[str, Any]:
    return facade.collect_korean_news(category, limit, force_refresh)
```

- [ ] **Step 4: crawler-worker 회귀 테스트를 실행함**

Run: `python3 tests/run_service_tests.py --suite crawler-worker`

Expected: archive 보존·중복 제거·알림 정책·검색 API 테스트가 통과함.

- [ ] **Step 5: 작업을 커밋함**

Run: `git commit -am "refactor: 뉴스 보관 서비스 책임 분리"`

### Task 4: 통합 검증과 PR

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
