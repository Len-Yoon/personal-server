# HomeOps 승인 기반 AI 운영 보조 시스템 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `crawler-worker`를 읽기 전용으로 진단하고 관리자 승인 뒤에만 재시작하며 복구 결과를 이력화함.

**Architecture:** `portal-web`은 진단·AI 제안·승인·이력을 조정하고 Docker 소켓에 접근하지 않음. `homeops-executor`만 Docker SDK를 사용해 고정 allowlist의 상태·로그 조회와 재시작을 수행함.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, SQLite, Docker SDK, Docker Compose, unittest.

**Spec:** `docs/superpowers/specs/2026-08-19-homeops-approved-operations-design.md`

## Global Constraints

- 기존 URL·포트·health endpoint·관리자 비밀번호 인증 흐름은 유지함.
- HomeOps는 공개 포트를 열지 않으며, `crawler-worker`의 `restart_container`만 허용함.
- Docker 소켓은 `homeops-executor`에만 마운트함.
- 셸 명령, 파일·네트워크·이미지·컨테이너 생성 또는 삭제, 자동 실행은 구현하지 않음.
- AI 오류·스키마 오류·정책 위반은 `no_action`으로 기록하며 executor를 호출하지 않음.

---

## File Structure

| 파일 | 책임 |
|---|---|
| `homeops-executor/app/main.py` | 비공개 실행기 API·공유 비밀 검증 |
| `homeops-executor/app/services/docker_ops.py` | 고정 allowlist 상태·로그·restart |
| `portal-web/app/services/homeops.py` | SQLite 이력, AI 제안, 승인 토큰, 실행·검증 조정 |
| `portal-web/app/routers/dashboard.py` | HomeOps 관리자 라우트 |
| `portal-web/app/templates/admin_status.html` | 조치안·승인·이력 UI |
| `docker-compose*.yml` | executor 내부 배포와 소켓 격리 |
| `scripts/deploy-n100.sh`, `scripts/windows-bootstrap.sh` | 기존 명시 기동 목록에 executor 추가 |
| `tests/homeops_executor/test_docker_ops.py`, `tests/test_homeops.py` | 정책·상태 전이·복구 검증 |

### Task 1: 제한 Docker 실행기

**Files:**
- Create: `homeops-executor/app/main.py`, `homeops-executor/app/services/docker_ops.py`, `homeops-executor/requirements.txt`, `homeops-executor/Dockerfile`
- Test: `tests/homeops_executor/test_docker_ops.py`

**Interfaces:**
- Produces: `GET /health`, `GET /v1/diagnostics/{service}`, `POST /v1/restarts`
- Restart input: `{ "incident_id": "uuid", "approval_token": "token", "action": "restart_container", "service": "crawler-worker" }`

- [ ] **Step 1: Write failing allowlist tests**

```python
def test_outside_allowlist_is_rejected(self):
    with self.assertRaises(ValueError):
        docker_ops.collect_diagnostics("portal-web")

def test_restart_calls_docker_only_for_crawler(self):
    result = docker_ops.restart_service("crawler-worker", client=FakeDockerClient())
    self.assertEqual(result["service"], "crawler-worker")
```

- [ ] **Step 2: Verify the tests fail**

Run: `PYTHONPATH=homeops-executor python3 -m unittest tests.homeops_executor.test_docker_ops -v`

Expected: FAIL because executor modules do not exist.

- [ ] **Step 3: Implement fixed Docker operations**

```python
ALLOWED_SERVICES = frozenset({"crawler-worker"})

def restart_service(service: str, client=None) -> dict[str, object]:
    _require_allowed_service(service)
    container = (client or docker.from_env()).containers.get(service)
    container.restart(timeout=10)
    container.reload()
    return _container_snapshot(service, container)
```

Implement diagnostics using only container get, inspect, logs (`tail=100`, maximum 32 KiB), and health response. Require `X-HomeOps-Executor-Secret` for every `/v1/*` request.

- [ ] **Step 4: Verify the tests pass and commit**

Run: `PYTHONPATH=homeops-executor python3 -m unittest tests.homeops_executor.test_docker_ops -v`

Expected: PASS.

Commit: `git add homeops-executor tests/homeops_executor && git commit -m "feat: HomeOps 제한 Docker 실행기 추가"`

### Task 2: 승인·이력·AI 안전 실패 처리

**Files:**
- Create: `portal-web/app/services/homeops.py`
- Test: `tests/test_homeops.py`

**Interfaces:**
- Produces: `create_diagnosis(service)`, `approve_incident(incident_id, approved_by)`, `execute_approved_incident(incident_id)`, `list_incidents(limit=20)`.

- [ ] **Step 1: Write failing state-machine tests**

```python
def test_unapproved_incident_cannot_restart(self):
    incident = self.service.create_diagnosis("crawler-worker")
    self.assertEqual(self.service.execute_approved_incident(incident["incident_id"])["status"], "failed")
    self.assertEqual(self.executor.restart_calls, [])

def test_approval_is_single_use_and_health_is_verified(self):
    incident = self.service.create_diagnosis("crawler-worker")
    self.service.approve_incident(incident["incident_id"], "admin")
    self.assertEqual(self.service.execute_approved_incident(incident["incident_id"])["status"], "verified")
```

- [ ] **Step 2: Verify the tests fail**

Run: `PYTHONPATH=portal-web python3 -m unittest tests.test_homeops -v`

Expected: FAIL because `app.services.homeops` does not exist.

- [ ] **Step 3: Implement persistent policy state**

Create `incidents`, `incident_events`, `approval_tokens` SQLite tables with parameterized SQL. Hash `secrets.token_urlsafe(32)` approval tokens; expire after 300 seconds and consume once with `UPDATE ... WHERE consumed_at IS NULL`. Mask `Authorization`, `Bearer`, password, token, and API-key values before storage. Allow only the service/action constants from Task 1.

Implement an optional OpenAI-compatible JSON API request with `urllib.request`; a missing key, response error, invalid JSON, missing fields, or policy mismatch must store this response and stop execution:

```python
{"action": "no_action", "requires_approval": false, "risk_level": "high", "summary": "AI 제안 사용 불가", "evidence": []}
```

- [ ] **Step 4: Add failed verification and masking tests**

```python
def test_failed_health_is_recorded_without_retry(self):
    self.executor.health_ok = False
    incident = self.service.create_diagnosis("crawler-worker")
    self.service.approve_incident(incident["incident_id"], "admin")
    self.assertEqual(self.service.execute_approved_incident(incident["incident_id"])["status"], "failed")

def test_secret_is_masked_before_persistence(self):
    self.executor.logs = ["Authorization: Bearer secret-value"]
    incident = self.service.create_diagnosis("crawler-worker")
    self.assertNotIn("secret-value", str(incident))
```

- [ ] **Step 5: Verify the tests pass and commit**

Run: `PYTHONPATH=portal-web python3 -m unittest tests.test_homeops -v`

Expected: PASS.

Commit: `git add portal-web/app/services/homeops.py tests/test_homeops.py && git commit -m "feat: HomeOps 승인 및 장애 이력 추가"`

### Task 3: 관리자 화면과 인증 연결

**Files:**
- Modify: `portal-web/app/routers/dashboard.py`, `portal-web/app/templates/admin_status.html`, `portal-web/app/static/css/style.css`
- Modify: `tests/test_portal_dashboard.py`

**Interfaces:**
- Produces: `POST /admin/homeops/diagnose`, `POST /admin/homeops/{incident_id}/approve`, `POST /admin/homeops/{incident_id}/execute`, `GET /admin/homeops/history`.

- [ ] **Step 1: Write failing UI and Origin tests**

```python
def test_admin_page_contains_homeops_actions(self):
    response = self._authenticated_admin_response()
    self.assertIn("HomeOps 승인 운영", response.text)
    self.assertIn('action="/admin/homeops/diagnose"', response.text)

def test_cross_origin_execute_is_rejected(self):
    response = client.post("/admin/homeops/id/execute", headers={"Origin": "https://evil.example"})
    self.assertEqual(response.status_code, 403)
```

- [ ] **Step 2: Verify the tests fail**

Run: `PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard -v`

Expected: FAIL because HomeOps markup and routes do not exist.

- [ ] **Step 3: Implement authenticated admin routes and UI**

Reuse `_require_security_password()`, existing Origin middleware, `append_security_event()`, and `_disable_cache()`. Show structured summary, evidence, risk, impact, verification checklist, status, and 20 recent records. Allow approval only from `proposed`, execution only from `approved`; use no free-form service or command inputs.

- [ ] **Step 4: Verify the tests pass and commit**

Run: `PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard tests.test_homeops -v`

Expected: PASS.

Commit: `git add portal-web/app/routers/dashboard.py portal-web/app/templates/admin_status.html portal-web/app/static/css/style.css tests/test_portal_dashboard.py && git commit -m "feat: 관리자 HomeOps 승인 화면 추가"`

### Task 4: 내부 배포 및 N100 연결

**Files:**
- Modify: `docker-compose.yml`, `docker-compose.n100.yml`, `scripts/deploy-n100.sh`, `scripts/windows-bootstrap.sh`, `.env.example`
- Modify: `tests/test_compose_config.py`, `tests/test_deploy_n100.py`

- [ ] **Step 1: Write failing deployment isolation tests**

```python
def test_only_executor_mounts_docker_socket(self):
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    self.assertIn("homeops-executor:", compose)
    self.assertIn("/var/run/docker.sock:/var/run/docker.sock", compose)

def test_deployment_starts_executor(self):
    self.assertIn("homeops-executor", SCRIPT)
```

- [ ] **Step 2: Verify the tests fail**

Run: `python3 -m unittest tests.test_compose_config tests.test_deploy_n100 -v`

Expected: FAIL because executor is absent.

- [ ] **Step 3: Implement private 64MB executor deployment**

Add `homeops-executor` with no `ports`, `restart: unless-stopped`, 64MB memory, 64 PID cap, read-only root, dropped capabilities, `no-new-privileges`, `/tmp` tmpfs, and 2×5MB log rotation. Pass its shared secret only to portal and executor. Add it to the two existing explicit startup lists while retaining every existing service and argument.

- [ ] **Step 4: Verify configuration and tests, then commit**

Run: `docker compose -f docker-compose.yml -f docker-compose.n100.yml config --quiet && python3 -m unittest tests.test_compose_config tests.test_deploy_n100 -v`

Expected: PASS.

Commit: `git add docker-compose.yml docker-compose.n100.yml scripts/deploy-n100.sh scripts/windows-bootstrap.sh .env.example tests/test_compose_config.py tests/test_deploy_n100.py && git commit -m "feat: N100 HomeOps 실행기 배포 구성 추가"`

### Task 5: 운영 문서와 전체 검증

**Files:**
- Modify: `docs/operations-reference.md`, `docs/agent-handoff.md`

- [ ] **Step 1: Document the permitted flow and exclusions**

Document `진단 → AI 제안 → 승인 → crawler-worker 재시작 → 검증 → 이력`, along with prohibited shell, deletion, network configuration, image/build, `system-agent`, `caddy`, and deployment actions.

- [ ] **Step 2: Run full focused verification**

Run: `PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard tests.test_portal_security tests.test_homeops -v && PYTHONPATH=system-agent python3 -m unittest tests.system_agent.test_metrics -v && PYTHONPATH=homeops-executor python3 -m unittest tests.homeops_executor.test_docker_ops -v && python3 -m unittest tests.test_compose_config tests.test_deploy_n100 -v && python3 -m compileall portal-web/app system-agent/app homeops-executor/app && docker compose -f docker-compose.yml -f docker-compose.n100.yml config --quiet && git diff --check`

Expected: all commands exit 0.

- [ ] **Step 3: Commit**

Commit: `git add docs/operations-reference.md docs/agent-handoff.md && git commit -m "docs: HomeOps 승인 운영 절차 추가"`

## Plan Self-Review

- 권한 분리(Task 1·4), AI 안전 실패(Task 2), 승인 UI(Task 3), 복구 검증·이력(Task 2), N100 연동(Task 4), 운영 인수인계(Task 5)를 모두 포함함.
- `crawler-worker`와 `restart_container` 이외의 실행 경로를 정의하지 않음.
- 모든 작업은 실패 테스트, 최소 구현, 통과 검증, 독립 커밋 순서를 가짐.
