# 전체 검색 상태 및 뉴스 수집 최신성 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Portal 검색의 서비스별 응답 상태와 News Hub 수집 최신성 정보를 사용자에게 표시함.

**Architecture:** Portal은 downstream마다 결과와 상태를 독립 반환해 부분 장애에서도 다른 결과를 보존함. Crawler Worker는 기존 `NewsCollectionStatusStore` snapshot을 읽기 전용 API와 홈 화면 context에서 재사용함.

**Tech Stack:** Python 3, FastAPI, Jinja2, unittest, urllib.request.

**Spec:** `docs/superpowers/specs/2026-09-18-search-status-news-freshness-design.md`

## Global Constraints

- 화면과 API에 예외 상세·내부 endpoint·호스트명을 노출하지 않음.
- scheduler, PVC, Secret, Telegram, Prometheus 메트릭, K3s manifest는 수정하지 않음.
- 내부 시각은 UTC ISO 8601을 유지하고 화면에는 KST `YYYY-MM-DD HH:MM`만 표시함.
- production code 전에는 반드시 failing test를 작성·실행함.

---

### Task 1: Portal 전체 검색의 서비스별 상태

**Files:**
- Modify: `portal-web/app/services/global_search.py`
- Modify: `portal-web/app/routers/dashboard.py`
- Modify: `portal-web/app/templates/dashboard.html`
- Modify: `tests/test_portal_dashboard.py`

**Interfaces:**
- Produces: `search_all(...) -> dict[str, dict[str, object]]`. 각 서비스 값은 `{"items": list[dict[str, Any]], "status": "ok" | "unavailable"}`임.
- Consumes: dashboard template은 service payload의 `items`, `status`만 사용함.

- [ ] **Step 1: failing test를 작성함**

`tests/test_portal_dashboard.py`에 한 endpoint `OSError`에도 다른 endpoint 결과가 유지되고 실패 서비스만 `{"items": [], "status": "unavailable"}`가 되는 테스트를 작성함. 정상 빈 payload는 `status="ok"`인 별도 테스트와, template의 `현재 응답 없음` 표시 테스트를 작성함.

- [ ] **Step 2: RED를 확인함**

Run: `python3 -m unittest tests.test_portal_dashboard.PortalDashboardTests.test_search_all_keeps_partial_results_when_one_service_is_unavailable`

Expected: FAIL because existing search returns lists only.

- [ ] **Step 3: 최소 구현을 작성함**

`_fetch_results()`와 demo 결과를 status payload로 바꾸고, `urlopen`·JSON·payload 오류는 `unavailable`로 변환함. template은 unavailable에만 `현재 응답 없음`, 정상 빈 목록에는 기존 결과 없음 문구를 표시함.

- [ ] **Step 4: GREEN을 확인함**

Run: `python3 -m unittest tests.test_portal_dashboard`

Expected: PASS.

- [ ] **Step 5: commit함**

Run: `git add portal-web/app/services/global_search.py portal-web/app/routers/dashboard.py portal-web/app/templates/dashboard.html tests/test_portal_dashboard.py && git commit -m "feat: 전체 검색 서비스 상태 표시 추가"`

### Task 2: News Hub 수집 최신성 API 및 화면

**Files:**
- Modify: `crawler-worker/app/routers/news.py`
- Modify: `crawler-worker/app/templates/home.html`
- Modify: `tests/crawler_worker/test_news_routes.py`

**Interfaces:**
- Consumes: `NewsCollectionStatusStore.snapshot()`의 기존 상태 키.
- Produces: `GET /api/collection-status`는 `initialized`, `last_attempt_at`, `last_success_at`, `consecutive_failures`만 반환함.
- Produces: home template의 `collection_status`는 KST 표시 문자열과 사용자 상태 메시지를 포함함.

- [ ] **Step 1: failing test를 작성함**

`tests/crawler_worker/test_news_routes.py`에 patched snapshot으로 API가 정확히 네 필드만 반환하는 테스트를 작성함. 홈은 KST 마지막 성공 시각·`최근 수집 실패 2회`·성공 기록 없음 문구를 각각 표시하는 테스트를 작성함.

- [ ] **Step 2: RED를 확인함**

Run: `python3 -m unittest tests.crawler_worker.test_news_routes.CrawlerWorkerNewsRouteTests.test_collection_status_api_returns_secret_free_snapshot`

Expected: FAIL with 404.

- [ ] **Step 3: 최소 구현을 작성함**

`news.py`에서 store snapshot의 허용 네 key만 복사하는 helper와 KST 표시 helper를 추가함. home route가 `collection_status`를 전달하고 template이 내부 UTC 원문 없이 최신성과 실패 수를 표시하게 함.

- [ ] **Step 4: GREEN을 확인함**

Run: `python3 -m unittest tests.crawler_worker.test_news_routes tests.crawler_worker.test_news_collection_status`

Expected: PASS.

- [ ] **Step 5: commit함**

Run: `git add crawler-worker/app/routers/news.py crawler-worker/app/templates/home.html tests/crawler_worker/test_news_routes.py && git commit -m "feat: 뉴스 수집 최신성 표시 추가"`

### Task 3: 통합 검증과 독립 검토

**Files:**
- Modify: focused tests only when Task 1 또는 Task 2 검증에서 필요한 회귀 assertion이 발견된 경우.

**Interfaces:**
- Consumes: Task 1 search payload와 Task 2 collection status API.
- Produces: scheduler, PVC, Secret, Telegram, Prometheus, K3s 변경 없는 검증 결과.

- [ ] **Step 1: 변경 경로 하네스를 실행함**

Run: `git diff --name-status -z --find-renames 64e2480 HEAD > /private/tmp/search-freshness-paths.z` then `python3 scripts/run_change_harness.py --input /private/tmp/search-freshness-paths.z --input-format git-name-status-z --agent-context`

Expected: blocked path 없음.

- [ ] **Step 2: 관련 회귀 검사를 실행함**

Run: `python3 -m unittest tests.test_portal_dashboard tests.crawler_worker.test_news_routes tests.crawler_worker.test_news_collection_status`

Expected: PASS.

- [ ] **Step 3: 정적 검사와 결과 하네스를 실행함**

Run: `git diff --check 64e2480 HEAD` then `python3 scripts/run_change_harness.py --input /private/tmp/search-freshness-paths.z --input-format git-name-status-z --check-result portal=success --check-result crawler-worker=success --check-result maintenance=success --agent-context`.

Expected: `ready_for_review`.

- [ ] **Step 4: 독립 전체 diff 검토를 요청함**

검토자는 부분 장애 처리, 사용자 노출 정보, KST 표시, 금지된 scheduler/PVC/Secret/Telegram/Prometheus/K3s 경로 변경을 확인함.
