# HomeOps 전체 상태 요약 및 일괄 재시작 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** HomeOps를 전체 서비스 상태 한 건 요약과 전체 재시작 한 번 실행으로 단순화함.

**Architecture:** Docker 권한은 `homeops-executor`에 유지함. 실행기는 전체 진단·일괄 재시작을 제공하고, 포털은 SQLite 단일 요약을 저장해 리다이렉트 후에도 상태를 보여줌. 실행기 자기 재시작 연결 종료는 health 재확인으로 처리함.

**Tech Stack:** Python 3.12, FastAPI, Docker SDK, SQLite, unittest, Jinja2

**Spec:** `docs/superpowers/specs/2026-08-24-homeops-summary-restart-design.md`

## Global Constraints

- 기존 allowlist 서비스만 대상으로 함.
- 관리자 세션 인증과 same-origin 검증을 유지함.
- 서버 기동 스크립트와 Windows 정기 스케줄러는 수정하지 않음.
- `homeops-executor`는 마지막에 재시작하고 재접속 확인으로 최종 상태를 판정함.

---

### Task 1: 실행기 전체 진단·일괄 재시작 API

**Files:**
- Modify: `homeops-executor/app/services/docker_ops.py`
- Modify: `homeops-executor/app/main.py`
- Test: `tests/homeops_executor/test_docker_ops.py`

**Interfaces:**
- Produces: `collect_all_diagnostics(client: Any | None = None) -> list[dict[str, object]]`
- Produces: `restart_all_services(client: Any | None = None) -> list[dict[str, object]]`
- Produces: `GET /v1/diagnostics`, `POST /v1/restarts/all`

- [ ] **Step 1: Write the failing tests**

```python
def test_all_diagnostics_returns_allowlist_in_name_order(self):
    result = docker_ops.collect_all_diagnostics(client=client)
    self.assertEqual([item["service"] for item in result], sorted(docker_ops.ALLOWED_SERVICES))

def test_restart_all_places_executor_last(self):
    result = docker_ops.restart_all_services(client=client)
    self.assertEqual(result[-1]["service"], "homeops-executor")
```

- [ ] **Step 2: Run the failing executor tests**

Run: `PYTHONPATH=homeops-executor python3 -m unittest tests.homeops_executor.test_docker_ops`

Expected: FAIL because the fleet helpers and endpoints do not exist.

- [ ] **Step 3: Implement minimal allowlist-only endpoints**

```python
def restart_all_services(client=None):
    names = sorted(ALLOWED_SERVICES - {"homeops-executor"}) + ["homeops-executor"]
    return [restart_service(name, client=client) for name in names]
```

Add authenticated all-diagnostics and restart-all FastAPI routes; retain existing single-service routes for the unchanged scheduler path.

- [ ] **Step 4: Verify the executor tests pass**

Run: `PYTHONPATH=homeops-executor python3 -m unittest tests.homeops_executor.test_docker_ops`

Expected: PASS.

### Task 2: 포털 단일 요약과 자기 재시작 복구 확인

**Files:**
- Modify: `portal-web/app/services/homeops.py`
- Test: `tests/test_homeops.py`

**Interfaces:**
- Produces: `HomeOpsService.diagnose_all() -> dict[str, object]`
- Produces: `HomeOpsService.restart_all() -> dict[str, object]`
- Produces: `HomeOpsService.latest_summary() -> dict[str, object] | None`

- [ ] **Step 1: Write the failing tests**

```python
def test_diagnose_all_groups_healthy_and_unhealthy_services(self):
    summary = self.service.diagnose_all()
    self.assertEqual(summary["healthy"], ["crawler-worker"])
    self.assertEqual(summary["unhealthy"], [{"service": "caddy", "reason": "healthcheck 비정상"}])

def test_restart_all_recovers_from_executor_connection_reset(self):
    self.executor.restart_all.side_effect = OSError("connection reset")
    self.executor.all_diagnostics.return_value = healthy_diagnostics
    self.assertEqual(self.service.restart_all()["failed"], [])
```

- [ ] **Step 2: Run the failing portal service tests**

Run: `PYTHONPATH=portal-web python3 -m unittest tests.test_homeops`

Expected: FAIL because single summary and reconnect handling do not exist.

- [ ] **Step 3: Implement persistence and normalized state**

Add a singleton SQLite table for the latest HomeOps summary. Normalize only `중지됨`, `healthcheck 비정상`, and `실행기 응답 없음`. On executor restart connection closure, poll bounded diagnostics and record recovery outcome instead of raising the transport error.

- [ ] **Step 4: Verify portal service tests pass**

Run: `PYTHONPATH=portal-web python3 -m unittest tests.test_homeops tests.test_homeops_notifier`

Expected: PASS.

### Task 3: 요약 전용 포털 라우트와 화면

**Files:**
- Modify: `portal-web/app/routers/admin.py`
- Modify: `portal-web/app/templates/admin_status.html`
- Modify: `portal-web/app/static/css/style.css`
- Test: `tests/test_homeops.py`
- Test: `tests/test_portal_dashboard.py`

**Interfaces:**
- Consumes: `diagnose_all()`, `restart_all()`, `latest_summary()`
- Produces: `POST /admin/homeops/diagnose`, `POST /admin/homeops/restart-all`

- [ ] **Step 1: Write the failing route/rendering tests**

```python
def test_homeops_renders_a_compact_single_summary(self):
    response = client.post("/admin/homeops/diagnose", headers={"Origin": "http://testserver"})
    self.assertIn("정상: crawler-worker", response.text)
    self.assertIn("비정상: caddy", response.text)
    self.assertNotIn("최근 조치 이력", response.text)
```

- [ ] **Step 2: Run the failing portal UI tests**

Run: `PYTHONPATH=portal-web python3 -m unittest tests.test_homeops tests.test_portal_dashboard`

Expected: FAIL because the summary-only UI and global restart route do not exist.

- [ ] **Step 3: Implement minimal authenticated UI**

Replace service select, incident history, approval and per-service execution forms with one diagnosis form, one `전체 재시작` form, compact healthy/unhealthy lines, and reasons for abnormal services only. Keep same-origin authorization on both POST routes.

- [ ] **Step 4: Verify portal UI tests pass**

Run: `PYTHONPATH=portal-web python3 -m unittest tests.test_homeops tests.test_portal_dashboard tests.test_portal_security`

Expected: PASS.

### Task 4: Integrated verification and review

**Files:**
- Test only: affected portal and executor test modules

- [ ] **Step 1: Run affected service suites**

Run: `PYTHONPATH=portal-web python3 -m unittest tests.test_homeops tests.test_homeops_notifier tests.test_portal_dashboard tests.test_portal_security && PYTHONPATH=homeops-executor python3 -m unittest tests.homeops_executor.test_docker_ops`

Expected: PASS.

- [ ] **Step 2: Run policy and compose tests**

Run: `python3 -m unittest tests.test_compose_config tests.test_deploy_n100`

Expected: PASS; no server startup or scheduler file changes.

- [ ] **Step 3: Inspect scope and request independent review**

Run: `git diff --check && git diff --stat main...HEAD`

Require independent review of allowlist preservation, same-origin authorization, executor self-restart recovery, and excluded startup/scheduler areas before creating a PR.
