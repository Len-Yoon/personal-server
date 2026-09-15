# N100 Security Recovery Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** N100의 실제 Tunnel 연결 단절을 감지하고, 감시·외부 알림·HomeOps 복구의 단일 실패 지점을 줄임.

**Architecture:** Windows Supervisor는 Daemon을 단일 관리하고 Daemon은 3분 주기로 제한형 복구를 수행함. Tunnel health는 로컬 service·process·NodePort와 공개 `/health`를 조합해 판정함. GitHub Actions는 health state를 Telegram delivery와 분리하며, HomeOps는 전용 공유 비밀값 없이는 fail-closed 처리함.

**Tech Stack:** PowerShell, Windows Task Scheduler, WSL, GitHub Actions, Python/FastAPI, Python unittest.

**Spec:** `docs/superpowers/specs/2026-09-10-security-recovery-phase-1-design.md`

## Global Constraints

- Portal PVC·Secret·운영 데이터·Caddyfile·Tunnel ingress·Compose Portal writer는 수정하지 않음.
- 새 sudo 권한·비밀번호·Telegram token·외부 자격증명을 생성·출력·저장하지 않음.
- N100 자동복구는 기존 KeepAlive 시작, inactive K3s 시작, 제한형 Portal rollout restart, Tunnel 재기동 범위를 넘지 않음.
- 모든 실제 외부 health 검증은 `https://len.pe.kr/health`를 10초 간격으로 3회 호출해 HTTP 200을 확인함.
- HomeOps 전용 비밀값이 운영 환경에 사전 설정되지 않았으면 해당 변경을 배포하지 않음.

---

### Task 1: Tunnel 실제 연결 판정과 Supervisor telemetry 격리

**Files:**
- Modify: `scripts/windows-bootstrap.ps1:239-347,871-922`
- Modify: `tests/test_windows_bootstrap.py`

**Interfaces:**
- Produces: `Test-PublicPortalHealth`와 로컬 NodePort가 정상일 때만 public health를 반영하는 Tunnel health 판정.
- Produces: telemetry 예외를 격리하고 Daemon을 계속 기동하는 `Start-Supervisor`.

- [ ] **Step 1: 실패하는 Tunnel·Supervisor 계약 테스트를 추가함**

`tests/test_windows_bootstrap.py`에 공개 health probe의 제한 시간, NodePort 정상 조건, telemetry 예외 격리 조건을 검사하는 테스트를 추가함.

- [ ] **Step 2: 새 테스트가 현재 코드에서 실패하는지 확인함**

Run: `python3 -m unittest tests.test_windows_bootstrap -v`

Expected: 공개 health 판정 또는 Supervisor telemetry 예외 격리 계약이 없어서 실패함.

- [ ] **Step 3: 최소 PowerShell 구현을 추가함**

`Test-PublicPortalHealth`는 기존 timeout wrapper로 WSL `curl --fail --silent --show-error --max-time 15 https://len.pe.kr/health`만 호출함. `Get-RecoveryHealth`는 NodePort가 healthy이고 service·process가 정상일 때 이 probe도 성공해야 Tunnel을 healthy로 설정함. `Start-Supervisor`는 `Update-HostMetrics`만 별도 try/catch로 감싸고 이후 대기·bootstrap·Daemon loop를 계속 실행함.

- [ ] **Step 4: 자동복구 계약 테스트를 통과시킴**

Run: `python3 -m unittest tests.test_windows_bootstrap -v`

Expected: PASS.

### Task 2: Telegram 설정과 외부 health 상태 분리

**Files:**
- Modify: `.github/workflows/public-uptime-monitor.yml:20-175`
- Modify: `tests/test_public_uptime_monitor.py`

**Interfaces:**
- Produces: Telegram Secret 누락 시에도 health check와 incident state 처리.
- Produces: 전송되지 않은 incident의 안전한 종료 증적.

- [ ] **Step 1: 실패하는 workflow 계약 테스트를 추가함**

Secret 검사가 health 이전에 workflow를 종료하지 않고, 전송 단계만 Secret 누락을 처리하며, 알림 미전송 incident가 복구 시 닫히는 문자열 계약을 추가함.

- [ ] **Step 2: 새 테스트가 현재 workflow에서 실패하는지 확인함**

Run: `python3 -m unittest tests.test_public_uptime_monitor -v`

Expected: Secret 사전 종료 계약 때문에 실패함.

- [ ] **Step 3: workflow를 최소 변경함**

초기 Secret 검사 단계를 제거하고 Telegram 전송 step에 비밀값 존재 검사를 둠. health success에서 down marker가 없는 open incident는 Telegram 복구 전송 없이 안전한 “알림 미전송” 증적과 함께 닫음. Secret·응답 본문은 출력하지 않음.

- [ ] **Step 4: 공개 monitor 계약 테스트를 통과시킴**

Run: `python3 -m unittest tests.test_public_uptime_monitor -v`

Expected: PASS.

### Task 3: HomeOps 전용 공유 비밀값 fail-closed

**Files:**
- Modify: `portal-web/app/services/homeops.py`
- Modify: `homeops-executor/app/main.py`
- Modify: `tests/test_homeops.py`
- Modify: `tests/homeops_executor/test_docker_ops.py`
- Modify: `docs/operations-reference.md`

**Interfaces:**
- Consumes: 운영자가 사전 설정한 비어 있지 않은 `HOMEOPS_EXECUTOR_SHARED_SECRET`.
- Produces: 관리자 비밀번호 fallback 없이 403으로 거부하는 Portal·Executor HomeOps API.

- [ ] **Step 1: 실패하는 전용 비밀값 테스트를 추가함**

Portal `ExecutorClient`와 Executor API가 `HOMEOPS_EXECUTOR_SHARED_SECRET`이 비어 있고 `ADMIN_STATUS_PASSWORD`만 있는 경우 비밀값을 재사용하지 않고 403을 반환하는 테스트를 추가함.

- [ ] **Step 2: 새 테스트가 현재 fallback 구현에서 실패하는지 확인함**

Run: `python3 -m unittest tests.test_homeops tests.homeops_executor.test_docker_ops -v`

Expected: 관리자 비밀번호 fallback 때문에 실패함.

- [ ] **Step 3: fallback을 제거하고 fail-closed 처리함**

Portal `ExecutorClient`와 Executor `_executor_shared_secret`은 전용 환경 변수만 읽음. 누락 시 HTTP 요청을 보내지 않고 명확한 내부 설정 오류를 반환하며 Executor는 403을 반환함. 비밀값 원문을 로그·응답·상태에 포함하지 않음.

- [ ] **Step 4: HomeOps 관련 테스트를 통과시킴**

Run: `python3 -m unittest tests.test_homeops tests.homeops_executor.test_docker_ops -v`

Expected: PASS.

### Task 4: 운영 문서·검증·독립 검토

**Files:**
- Modify: `README.md`
- Modify: `docs/n100-mt4-setup.md`
- Modify: `docs/public-uptime-monitor.md`
- Modify: `docs/operations-reference.md`
- Modify: `tests/test_documentation_index.py`

**Interfaces:**
- Produces: 실제 Tunnel 판정, Telegram 분리, HomeOps 전용 비밀값 fail-closed 운영 기준.

- [ ] **Step 1: 문서 계약 테스트를 추가함**

새 운영 경계와 비밀값 금지 위치를 검증하는 문서 계약을 추가함.

- [ ] **Step 2: 문서 계약을 확인함**

Run: `python3 -m unittest tests.test_documentation_index -v`

Expected: PASS.

- [ ] **Step 3: 관련 문서만 갱신함**

README와 운영 문서에 실제 동작·한계·수동 검증 절차만 기록하고 token·chat ID·비밀번호는 기록하지 않음.

- [ ] **Step 4: 범위·정적·외부 검증을 수행함**

Run: `git diff --name-status -z --find-renames HEAD > /tmp/security-recovery-phase-1-changes.z && git diff --check && python3 scripts/verify_change_scope.py --input /tmp/security-recovery-phase-1-changes.z --input-format git-name-status-z && python3 scripts/run_change_harness.py --input /tmp/security-recovery-phase-1-changes.z --input-format git-name-status-z --check-result maintenance=success --agent-context`

Expected: 변경 범위 차단 없음, harness ready_for_review.

- [ ] **Step 5: 외부 health를 검증함**

Run: `for attempt in 1 2 3; do curl --silent --show-error --max-time 15 --write-out '%{http_code}\n' --output /dev/null https://len.pe.kr/health; [ "$attempt" -lt 3 ] && sleep 10; done`

Expected: 세 번 모두 `200`.

- [ ] **Step 6: 독립 보안·운영 검토와 사용자 승인 뒤 커밋·PR·병합함**

독립 검토에서 Docker socket 권한 경계, 비밀값 비노출, Tunnel 오판 방지, workflow state 전환, 금지 영역 미변경을 확인함. 사용자 승인 전에는 push·PR·병합하지 않음.
