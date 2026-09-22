# CI 감사 알림 재시도 안정화 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 월간 SRE 감사 설치 검증이 relay 상태를 다시 조회하기 전에 짧은 timeout으로 종료되는 간헐 실패를 제거함.

**Architecture:** relay 전달 확인은 첫 상태 조회 뒤 설정 가능한 간격으로 다시 조회함. 재조회 간격은 timeout보다 짧아야 하며, 각 조회 뒤 남은 시간보다 길면 남은 시간 안으로 줄임. 이를 벗어나거나 형식이 잘못되면 CronJob 활성화 전에 실패함. 기본 운영 timeout 2분과 전달 확인·CronJob 활성화 순서는 유지함.

**Tech Stack:** Bash, Python `unittest`, Kubernetes CLI 계약 테스트.

**Spec:** `docs/operations-roadmap.md` 및 `docs/20260921_프로젝트보완_개발계획.md`의 CI 검증·운영 보호 기준.

## Global Constraints

- 실제 K3s 리소스·CronJob·Secret·운영 데이터는 변경하지 않음.
- 기본 relay 전달 확인 timeout은 `2m`을 유지함.
- 전달 미확인은 CronJob 활성화를 계속 차단함.
- 변경은 테스트 fixture와 host-side 설치 스크립트로 한정함.

## Review Focus

- 2초 timeout에서 relay가 두 번째 조회에 전달 완료를 기록하면 활성화되어야 함.
- relay가 끝까지 전달 완료를 기록하지 않으면 활성화하면 안 됨.
- 기본 2분 timeout에서는 기존 대기 간격을 유지해야 함.
- duration 형식이 잘못되면 설치가 실패해야 함.
- 실제 설치 모드가 Secret을 읽거나 기록하면 안 됨.

---

### Task 1: Relay 재조회 간격 주입

**Files:**
- Modify: `infra/k8s/tools/quarterly-sre-audit-automation.sh:23-24,333-357`
- Modify: `tests/test_k8s_quarterly_sre_audit_automation.py:73-91,344-350`

**Interfaces:**
- Consumes: `QUARTERLY_SRE_AUDIT_RELAY_DELIVERY_TIMEOUT`의 duration 문자열.
- Produces: 선택 환경변수 `QUARTERLY_SRE_AUDIT_RELAY_DELIVERY_RETRY_SECONDS`; 양의 정수 초만 허용하며 미지정 시 `2`.

- [x] **Step 1: 실패하는 경계 테스트를 작성함.**

```python
def test_install_waits_for_relay_to_record_the_official_run_before_activation(self):
    result, calls = self.run_tool(
        "--install", relay_delivery="delivered_after_retry", relay_delivery_timeout="2s", relay_delivery_retry_seconds="1"
    )
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertGreaterEqual(calls.count("get configmap sre-telegram-relay-state"), 2)
```

- [x] **Step 2: 수정 전 테스트가 실패함을 확인함.**

Run: `python3 -m unittest -v tests.test_k8s_quarterly_sre_audit_automation.QuarterlySreAuditAutomationTests.test_install_waits_for_relay_to_record_the_official_run_before_activation`

Expected: 기존 2초 대기에서는 두 번째 relay 조회 전에 종료됨.

- [x] **Step 3: 최소 구현을 작성함.**

```bash
RELAY_DELIVERY_RETRY_SECONDS=${QUARTERLY_SRE_AUDIT_RELAY_DELIVERY_RETRY_SECONDS:-2}

relay_delivery_retry_seconds() {
  [[ "$RELAY_DELIVERY_RETRY_SECONDS" =~ ^[1-9][0-9]*$ ]] || return 1
  printf '%s\n' "$RELAY_DELIVERY_RETRY_SECONDS"
}
```

`verify_relay_delivery`는 해당 값을 읽고 `sleep`에 사용함. 값이 양의 정수가 아니거나 timeout 이상이면 전달 미확인과 같이 설치를 실패시킴. 각 상태 조회 뒤 남은 시간도 대기값에 반영함.

- [x] **Step 4: 경계·기본값·미전달·잘못된 retry 값 테스트를 실행함.**

Run: `python3 -m unittest -v tests.test_k8s_quarterly_sre_audit_automation`

Expected: relay가 두 번째 조회에서 전달되면 활성화되고, 미전달은 활성화하지 않으며, 기존 Secret 비접근 계약이 통과함.

- [x] **Step 5: 변경을 커밋함.**

```bash
git add infra/k8s/tools/quarterly-sre-audit-automation.sh tests/test_k8s_quarterly_sre_audit_automation.py
git commit -m "fix: SRE 감사 relay 재조회 간격 안정화"
```

### Task 2: K3s 계약 회귀 검증

**Files:**
- Modify: `docs/superpowers/plans/2026-09-22-ci-audit-retry-stability.md`

**Interfaces:**
- Consumes: Task 1의 retry 환경변수와 테스트 계약.
- Produces: 기본 운영 timeout과 fail-closed 전달 확인이 보존됐다는 검증 기록.

- [x] **Step 1: 전체 K3s 계약 테스트를 실행함.**

Run: `python3 -m unittest tests.test_k8s_monitoring_tools tests.test_k8s_sre_telegram_tools tests.test_k8s_quarterly_sre_audit_automation tests.test_k8s_quarterly_sre_audit_cronjob tests.test_k8s_slo_daily_evidence -v`

Expected: 모든 테스트 통과.

Result: 2026-09-22 현재 수정 커밋 기준 171개 테스트 통과함.

- [x] **Step 2: 변경 범위와 공백을 확인함.**

Run: `git diff --check && git diff --name-only HEAD~1`

Expected: 설치 스크립트·테스트·계획 파일만 변경됨.

- [x] **Step 3: 계획의 완료 상태를 갱신하고 커밋함.**

```bash
git add docs/superpowers/plans/2026-09-22-ci-audit-retry-stability.md
git commit -m "docs: CI 감사 재시도 안정화 검증 기록"
```

## Self-Review

- Spec coverage: 짧은 timeout 경계, 미전달 차단, 기본 운영값, 잘못된 입력, Secret 비접근을 Task 1~2에서 검증함.
- Placeholder scan: 없음.
- Type consistency: 환경변수는 Bash 문자열이며 양의 정수로 검증한 뒤 `sleep`에 전달함.
- Review Focus: 각 항목을 Task 1의 테스트와 Task 2의 계약 테스트에서 확인함.
