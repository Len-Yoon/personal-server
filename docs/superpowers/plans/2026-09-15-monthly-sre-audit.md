# 월간 SRE 통합 점검 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 내부 정기 복구 검증을 하나의 월간 SRE 감사로 통합하고, 백업 자격 증명 경계로 인한 수동 훈련 실패를 제거함.

**Architecture:** 기존 K3s 감사 runner와 최소 RBAC를 재사용해 새 월간 CronJob을 추가하고 이전 분기 CronJob은 suspended 상태로 유지함. Telegram relay의 기존 상태 ConfigMap 계약은 유지함. 수동 복구 훈련은 rclone을 호출하지 않고 Kubernetes ConfigMap의 최신 백업·복원 증적만 검사함. 공개 상태 감시와 일일 백업 실행기는 독립적으로 유지함.

**Tech Stack:** Bash, Kubernetes CronJob/RBAC/ConfigMap, Python unittest, YAML.

**Spec:** `docs/superpowers/specs/2026-09-15-monthly-sre-audit-design.md`

## Global Constraints

- 외부 공개 상태 감시와 일일 백업 실행 CronJob은 수정하지 않음.
- Secret, rclone 자격 증명, Telegram 자격 증명은 읽거나 기록하지 않음.
- Portal PVC, 운영 데이터, Caddy, Tunnel ingress를 수정하지 않음.
- 월간 감사는 단일 실행과 최소 RBAC를 유지함.

---

### Task 1: 백업 증적 전용 수동 점검 계약

**Files:**
- Modify: `infra/k8s/tools/monthly-recovery-drill.sh`
- Create: `infra/k8s/tools/check-portal-backup-evidence.sh`
- Test: `tests/test_monthly_recovery_drill.py`

**Interfaces:**
- Consumes: `personal-server/portal-pvc-backup-evidence` ConfigMap의 `evidence` 필드와 `validate-backup-evidence.py`.
- Produces: `portal_backup_evidence=PASS|FAIL` 및 월간 증적의 backup 단계 상태.

- [ ] **Step 1: 실패 테스트 작성**

`monthly-recovery-drill.sh`가 `--check` 인자를 가진 원격 백업 도구가 아니라 백업 증적 검사기를 호출한다고 검증함.

- [ ] **Step 2: 실패 확인**

Run: `python3 -m unittest tests.test_monthly_recovery_drill -v`

Expected: 기존 wrapper가 `portal-pvc-backup-verify.sh --check`를 기본으로 사용하므로 새 계약 검증이 실패함.

- [ ] **Step 3: 최소 구현**

in-cluster 또는 host `kubectl`에서 ConfigMap 증적을 임시 0600 파일로 읽고 `validate-backup-evidence.py --max-age-seconds 86400`로 검사하는 도구를 추가함. 원격 백업·Secret·rclone 호출을 추가하지 않음.

- [ ] **Step 4: 통과 확인**

Run: `python3 -m unittest tests.test_monthly_recovery_drill tests.test_validate_backup_evidence -v`

Expected: PASS.

### Task 2: 분기 감사의 월간 단일 실행 전환

**Files:**
- Modify: `infra/k8s/sre-audit-automation/quarterly-sre-audit-cronjob.yaml`
- Modify: `infra/k8s/tools/quarterly-sre-audit-automation.sh`
- Modify: `infra/k8s/tools/quarterly-sre-audit-runner.sh`
- Modify: `tests/test_k8s_quarterly_sre_audit_cronjob.py`
- Modify: `tests/test_k8s_quarterly_sre_audit_automation.py`

**Interfaces:**
- Consumes: 기존 감사 ConfigMap·ServiceAccount·RoleBinding 계약.
- Produces: 월 1회, `Forbid`, 수동 Job과 동일한 runner를 사용하는 활성 월간 감사 CronJob. 기존 분기 CronJob은 suspended 상태로 보존함.

- [ ] **Step 1: 실패 테스트 작성**

월간 일정, 활성 CronJob 하나, validation CronJob과 기존 분기 CronJob의 suspended 유지를 검증함.

- [ ] **Step 2: 실패 확인**

Run: `python3 -m unittest tests.test_k8s_quarterly_sre_audit_cronjob tests.test_k8s_quarterly_sre_audit_automation -v`

Expected: 현재 분기 일정과 이름 계약 때문에 실패함.

- [ ] **Step 3: 최소 구현**

CronJob·controller의 월간 리소스를 추가하고 runner·Telegram 상태 ConfigMap 계약은 유지함. 설치 시 수동 Job 성공 뒤에만 월간 CronJob을 활성화하고, 이전 분기 CronJob은 suspended로 남김. 새로운 권한이나 Secret 참조를 추가하지 않음.

- [ ] **Step 4: 통과 확인**

Run: `python3 -m unittest tests.test_k8s_quarterly_sre_audit_cronjob tests.test_k8s_quarterly_sre_audit_automation -v`

Expected: PASS.

### Task 3: 운영 문서와 안전 검증

**Files:**
- Modify: `docs/recovery-drill.md`
- Modify: `infra/k8s/README.md`
- Test: `tests/test_documentation_index.py`

- [ ] **Step 1: 실패 테스트 또는 문서 계약 검증 추가**

문서가 월간 단일 감사, 독립 외부 감시, 단일 백업 실행기 경계를 설명하도록 검증함.

- [ ] **Step 2: 실패 확인**

Run: `python3 -m unittest tests.test_documentation_index -v`

- [ ] **Step 3: 문서 갱신**

수동 실행, 결과 확인, 중단·rollback 절차를 최신 이름과 일정에 맞추고 자격 증명을 직접 실행하지 않음을 명시함.

- [ ] **Step 4: 통합 검증**

Run: `bash -n infra/k8s/tools/monthly-recovery-drill.sh infra/k8s/tools/check-portal-backup-evidence.sh infra/k8s/tools/quarterly-sre-audit-automation.sh infra/k8s/tools/quarterly-sre-audit-runner.sh && python3 -m unittest tests.test_monthly_recovery_drill tests.test_validate_backup_evidence tests.test_k8s_quarterly_sre_audit_cronjob tests.test_k8s_quarterly_sre_audit_automation tests.test_documentation_index -v`

Expected: PASS.

### Task 4: 변경 범위·독립 검토·운영 적용 준비

**Files:**
- Verify only: 변경된 파일 전체

- [ ] **Step 1: 변경 범위 harness 실행**

Run: `git diff --name-status -z --find-renames origin/main HEAD > /tmp/monthly-sre-audit-paths.z && python3 scripts/run_change_harness.py --input /tmp/monthly-sre-audit-paths.z --input-format git-name-status-z --agent-context`

- [ ] **Step 2: 정적 안전 경계 확인**

Run: `! rg -n 'rclone|PORTAL_RCLONE|Secret|secretKeyRef' infra/k8s/tools/check-portal-backup-evidence.sh infra/k8s/tools/monthly-recovery-drill.sh && echo credential_boundary=PASS`

- [ ] **Step 3: 독립 검토 및 결과 반영**

RBAC, schedule 중복, CronJob 전환 rollback, external monitor 독립 여부를 검토하고 발견 사항을 반영함.
