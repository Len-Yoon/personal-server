# 관리자 상태 실제 수집 시각 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 관리자 상태 화면이 매 조회 시 생성되는 API 시각 대신 Windows 호스트 메트릭의 실제 수집 시각을 표시함.

**Architecture:** system-agent와 스케줄러는 변경하지 않음. portal-web 라우터가 host.captured_at을 선택해 템플릿에 전달하고, 데이터가 없을 때는 unknown을 표시함.

**Tech Stack:** Python, FastAPI, Jinja2, unittest.

## Global Constraints

- 서버 기동 영역과 스케줄러 구현은 수정하지 않음.
- system-agent 응답 형식과 관리자 인증 정책을 변경하지 않음.
- host.captured_at이 비어 있으면 최상위 captured_at으로 대체하지 않음.
- 검증은 PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard -v로 수행함.

### Task 1: 실제 호스트 수집 시각 표시

**Files:**
- Modify: portal-web/app/routers/dashboard.py
- Modify: portal-web/app/templates/admin_status.html
- Modify: tests/test_portal_dashboard.py

**Interfaces:**
- Consumes: system_status["host"].get("captured_at", "").
- Produces: 템플릿 컨텍스트 status_checked_at: str. 유효한 호스트 수집 시각은 KST 형식, 값이 없으면 unknown.

- [ ] **Step 1: 실패하는 회귀 테스트를 작성함**

기존 test_admin_status_page_renders_formatted_collection_time의 mock 응답에 서로 다른 시각을 설정함.

```python
"captured_at": "2026-07-09T01:02:03+00:00",
"host": {
    "captured_at": "2026-07-09T00:40:00+00:00",
    "cpu_percent": None,
    "memory_percent": None,
    "disk_percent": None,
    "source": "windows",
},
```

응답은 09:40 KST를 포함하고 10:02 KST를 포함하지 않는다고 검증함. host.captured_at이 없는 별도 테스트에서는 화면에 unknown이 표시된다고 검증함.

- [ ] **Step 2: 테스트가 기존 동작에서 실패하는지 확인함**

Run:

```bash
PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard.PortalDashboardTests.test_admin_status_page_renders_host_collection_time -v
```

Expected: FAIL. 기존 라우터가 최상위 system_status captured_at을 표시함.

- [ ] **Step 3: 최소 구현을 추가함**

dashboard.py의 관리자 상태 TemplateResponse 컨텍스트에서 아래 값을 전달함.

```python
"status_checked_at": format_status_checked_at(
    str((system_status.get("host") or {}).get("captured_at", ""))
),
```

admin_status.html의 표시 문구를 수집 시각에서 호스트 수집 시각으로 변경함. 최상위 captured_at은 화면 표시 용도로 사용하지 않음.

- [ ] **Step 4: 집중 테스트와 관리자 상태 회귀 테스트를 실행함**

Run:

```bash
PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard -v
```

Expected: PASS.

- [ ] **Step 5: 변경을 커밋함**

```bash
git add portal-web/app/routers/dashboard.py portal-web/app/templates/admin_status.html tests/test_portal_dashboard.py
git commit -m "fix: 관리자 상태 실제 수집 시각 표시"
```
