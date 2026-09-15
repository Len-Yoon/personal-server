# Portal Backup Remote Preflight Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 일시적인 원격 저장소 응답 지연이 Portal PVC 백업을 실패시키지 않도록 timeout 사전점검만 제한 재시도함.

**Architecture:** `assert_remote_access()`는 자격 증명을 한 번 준비하고 `rclone lsd`의 exit 124에만 최대 한 번 재시도함. 다른 실패는 즉시 기존 오류 분류로 반환하며, 재시도 상태는 기존 private diagnostic 파일에만 남김.

**Tech Stack:** Bash, rclone, Python unittest

**Spec:** `docs/superpowers/specs/2026-09-12-backup-remote-preflight-retry-design.md`

## Global Constraints

- 시도당 timeout 30초, 추가 재시도 기본 1회, backoff 기본 5초를 유지함.
- retry 범위는 timeout exit 124만 허용함.
- upload/restore 및 Portal/PVC/CronJob/Secret/RBAC 변경 금지함.
- 비밀값·원격 설정·인증 문자열을 표준출력 또는 상태 ConfigMap에 기록하지 않음.

### Task 1: 원격 preflight 재시도와 회귀 계약

**Files:**
- Modify: `infra/k8s/tools/portal-pvc-backup-verify.sh`
- Modify: `tests/test_k8s_portal_pvc_backup_verify.py`

- [ ] **Step 1: 실패 테스트 작성**

첫 `lsd` timeout 후 성공, timeout 소진, 비timeout 오류 즉시 실패를 각각 검증함.

- [ ] **Step 2: RED 실행**

Run: `python3 -m unittest tests.test_k8s_portal_pvc_backup_verify -q`

- [ ] **Step 3: 최소 구현**

`PORTAL_RCLONE_PREFLIGHT_RETRY_COUNT`와 `PORTAL_RCLONE_PREFLIGHT_RETRY_BACKOFF_SECONDS`를 범위 검증하고, exit 124에만 제한된 backoff 재시도를 적용함.

- [ ] **Step 4: GREEN 및 관련 회귀 실행**

Run: `python3 -m unittest tests.test_k8s_portal_pvc_backup_verify tests.test_k8s_portal_pvc_backup_automation -q`

- [ ] **Step 5: 독립 검토**

retry가 writer pause 이전에만 실행되고 비밀값·비timeout 오류 계약을 유지하는지 검토함.
