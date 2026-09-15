# 분기 K3s SRE 점검 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** systemd 사용자 timer 대신 최소 권한 K3s CronJob으로 분기 K3s SRE 점검과 Telegram relay 상태 전달을 자동화함.

**Architecture:** 전용 runner 이미지가 Kubernetes API만 사용해 노드·Portal·백업 증적·전용 namespace 복구 실습을 수행함. Compose 상태는 보안상 Docker socket을 사용하지 않으며 이 자동 결과에서 제외함. 설치 도구는 기존 timer를 중지한 뒤 CronJob을 suspend 상태로 반입하고, 수동 Job 성공 후에만 활성화함.

**Tech Stack:** Bash, kubectl, Kubernetes CronJob/RBAC, existing Telegram SRE relay.

**Spec:** `docs/superpowers/specs/2026-09-12-quarterly-k3s-sre-audit-design.md`

## Global Constraints

- Docker socket, hostPath, privileged, sudo를 사용하지 않음.
- Portal PVC·Secret·Caddy·Tunnel·Compose Portal writer를 수정하지 않음.
- `Asia/Seoul`, quarterly first-day 03:30, `Forbid`, fail-closed를 유지함.
- 결과 ConfigMap에는 비밀값을 기록하지 않음.

---

### Task 1: Runner와 최소 권한 매니페스트

**Files:**
- Create: `infra/k8s/sre-audit-automation/Dockerfile`
- Create: `infra/k8s/sre-audit-automation/quarterly-sre-audit-cronjob.yaml`
- Create: `infra/k8s/tools/quarterly-sre-audit-runner.sh`
- Test: `tests/test_k8s_quarterly_sre_audit_cronjob.py`

- [ ] 실패 테스트로 schedule, Pod 보안 설정, RBAC 자원 범위, 금지된 Docker/hostPath/sudo 문자열을 고정함.
- [ ] runner가 `kubectl`로 K3s 상태·백업 증적·복구 실습을 수행하고 항상 결과 ConfigMap 기록을 시도하도록 구현함.
- [ ] CronJob, ServiceAccount, Role/Binding, ClusterRole/Binding, 전용 recovery namespace를 구현함.
- [ ] 대상 테스트를 실행함.

### Task 2: 설치·마이그레이션 경계

**Files:**
- Modify: `infra/k8s/tools/quarterly-sre-audit-automation.sh`
- Modify: `tests/test_k8s_quarterly_sre_audit_automation.py`
- Modify: `infra/k8s/README.md`

- [ ] 실패 테스트로 기존 user timer 비활성화, suspend-first 적용, 수동 Job 성공 뒤 활성화 조건을 고정함.
- [ ] installer가 기존 timer를 중복 없이 종료하고 CronJob 설치·상태 조회를 수행하도록 변경함.
- [ ] 운영 문서에 점검 범위와 Compose 별도 증적 원칙을 명시함.
- [ ] 관련 테스트와 maintenance suite를 실행함.

### Task 3: 독립 검토와 N100 운영 검증

**Files:**
- Modify: 위 Task 1~2 파일 중 검토에서 지적된 최소 범위

- [ ] 보안 검토로 RBAC, Secret/PVC 미접근, Docker 소켓 미노출을 확인함.
- [ ] 변경 경로 하네스에 대상 검사 결과를 기록함.
- [ ] N100에서 이미지 반입, CronJob 수동 Job 성공, relay 전달, 외부 health 3회 연속 200을 확인함.
