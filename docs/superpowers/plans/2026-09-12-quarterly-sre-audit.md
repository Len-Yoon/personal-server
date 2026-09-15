# 분기 SRE 점검 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 운영 서비스를 중단하지 않는 분기 SRE 점검과 기존 Telegram relay 결과 보고를 추가함.

**Architecture:** N100 사용자 systemd timer가 controller를 실행함. Controller는 상태 점검·백업 상태 확인·격리 Pod 복구 훈련의 결과를 `monitoring/sre-telegram-quarterly-audit-status` ConfigMap에 기록하고, 기존 relay가 Telegram 성공 응답 뒤에만 새 실행 ID를 저장하여 최소 1회 전달을 보장함. 전송 또는 저장 실패는 기존 polling loop의 상한형 지수 backoff(1→2→4→…→30초)로 재시도하며, 별도 1분·5분·15분·1시간 scheduler는 추가하지 않음.

**Tech Stack:** Bash, systemd user units, K3s ConfigMap/RBAC, Python stdlib HTTP relay, unittest.

**Spec:** `docs/superpowers/specs/2026-09-12-quarterly-sre-audit-design.md`

## Global Constraints

- Portal, Caddy, Cloudflare Tunnel, Compose 서비스를 stop, restart, scale, rollout하지 않음.
- 기존 `sudo -n k3s`만 사용하며 Secret·token·chat ID를 만들거나 출력하지 않음.
- 분기 실행은 1·4·7·10월 1일 03:30 KST이며, timer는 persistent 단일 실행이어야 함.
- Telegram에는 단계별 통과/실패와 종합 결과만 전달하며, 실행 세부값과 인프라 식별자는 전달하지 않음.
- N100 실제 적용은 저장소 구현과 별도 사용자 승인이 필요함.

---

### Task 1: 분기 점검 controller와 systemd timer

**Files:**
- Create: `infra/k8s/tools/quarterly-sre-audit-automation.sh`
- Create: `infra/k8s/sre-audit-automation/personal-server-quarterly-sre-audit.service.tmpl`
- Create: `infra/k8s/sre-audit-automation/personal-server-quarterly-sre-audit.timer.tmpl`
- Create: `tests/test_k8s_quarterly_sre_audit_automation.py`

**Consumes:** `sre-health-audit.sh`, `portal-pvc-backup-verify.sh --check`, `sre-pod-recovery-lab.sh --run`.

**Produces:** `monitoring/sre-telegram-quarterly-audit-status` ConfigMap with `run_id`, `status`, `completed_at`, `health_audit`, `backup_check`, `recovery_lab` keys. Each status value is only `passed` or `failed`; a `--preflight|--install|--run|--status` host controller contract.

- [ ] **Step 1: Write failing controller and unit contract tests**

Test that the controller accepts only `--preflight|--install|--run|--status`; `--run` uses all three existing tools, continues after one tool fails, applies exactly the six safe ConfigMap keys, and exits nonzero when any check or ConfigMap write fails. Test that install renders and verifies user units before enabling the one timer and never creates or reads credential values. Test that the service runs only controller `--run`, has a non-blocking lock and explicit start/stop limits, uses strict filesystem protections, and that the timer has the exact KST quarterly calendar and `Persistent=true`.

- [ ] **Step 2: Run the new test and verify it fails**

Run: `python3 -m unittest tests.test_k8s_quarterly_sre_audit_automation -v`

Expected: FAIL because the controller and unit templates do not exist.

- [ ] **Step 3: Implement the minimal controller and unit templates**

Implement the controller with `set -Eeuo pipefail`, a fixed repo-relative tool list, a safe UTC run ID, a per-check result collector, and a final ConfigMap apply through `sudo -n k3s kubectl`. Do not include command output in the ConfigMap. Add safe state/unit directory creation, `--preflight`, and `--install` that renders templates, performs `systemd-analyze --user verify`, then reloads and enables the one timer without creating or copying credentials. Create a `Type=oneshot` service with a non-blocking runtime lock, explicit start/stop limits, `PrivateTmp=yes`, `ProtectSystem=strict`, `ProtectHome=read-only`, and only the repository/state paths needed. Create a persistent quarterly timer at `*-01,04,07,10-01 03:30:00 Asia/Seoul`.

- [ ] **Step 4: Run focused tests and shell validation**

Run: `python3 -m unittest tests.test_k8s_quarterly_sre_audit_automation -v && bash -n infra/k8s/tools/quarterly-sre-audit-automation.sh`

Expected: PASS.

### Task 2: relay delivery contract과 최소 RBAC

**Files:**
- Modify: `sre-telegram-relay/app/main.py`
- Modify: `infra/k8s/sre-telegram/base.yaml`
- Modify: `infra/k8s/tools/sre-pod-recovery-lab.sh`
- Modify: `tests/test_k8s_sre_telegram_tools.py`
- Modify: `tests/test_k8s_sre_telegram_manifests.py`
- Modify: `tests/test_k8s_sre_pod_recovery_lab.py`

**Consumes:** Task 1 ConfigMap schema and new run IDs.

**Produces:** Relay message formatter and bounded persistent delivery store for quarterly audit IDs; RBAC read permission scoped to the one ConfigMap; a hardened isolated recovery-lab Pod manifest.

- [ ] **Step 1: Write failing relay and manifest tests**

Test valid six-key reports, rejection of missing/unsafe values, a concise success/failure Korean message, send failure retry through polling backoff, successful-send state persistence, and no message containing raw run IDs or tool output. Test that a state write failure after successful send returns retryable failure and can cause a rare duplicate on the next attempt. Test the relay Role uses `resourceNames: [sre-telegram-quarterly-audit-status]` with read-only verbs only. Extend the recovery-lab contract to require a digest-pinned image, `automountServiceAccountToken: false`, non-root execution, dropped capabilities, and a writable `emptyDir` only for `/tmp`.

- [ ] **Step 2: Run focused tests and verify the new assertions fail**

Run: `python3 -m unittest tests.test_k8s_sre_telegram_tools tests.test_k8s_sre_telegram_manifests -v`

Expected: FAIL until quarterly report support exists.

- [ ] **Step 3: Implement minimal relay support**

Read the ConfigMap via the existing Kubernetes client pattern. Accept only the fixed schema and `passed`/`failed` values. Generate one Korean status summary. Send first, then persist at most 128 delivered quarterly IDs independently from backup delivery IDs; a post-send persistence failure remains retryable and may cause a rare duplicate after response loss. Reuse the existing capped polling backoff and do not add a scheduler for 1분·5분·15분·1시간 intervals. Add one Role rule to the existing relay role for `configmaps/get` scoped only to the quarterly ConfigMap. Harden only the lab Pod manifest with a project-approved immutable busybox digest, `automountServiceAccountToken: false`, `runAsNonRoot`, `allowPrivilegeEscalation: false`, all Linux capabilities dropped, and `/tmp` `emptyDir`; retain its automatic cleanup behavior.

- [ ] **Step 4: Run focused relay tests**

Run: `python3 -m unittest tests.test_k8s_sre_telegram_tools tests.test_k8s_sre_telegram_manifests -v`

Expected: PASS.

### Task 3: 운영 문서와 통합 검증

**Files:**
- Modify: `infra/k8s/README.md`
- Modify: `docs/operations-roadmap.md`
- Modify: `tests/run_service_tests.py` only if its maintenance suite does not automatically include the new focused tests.

**Consumes:** Task 1 timer path and Task 2 relay contract.

**Produces:** 설치·수동 실행·검증·미적용 경계를 설명한 운영 문서.

- [ ] **Step 1: Add documentation assertions where existing indexing contracts require them**

Add a focused assertion only if an existing documentation test requires the new automation reference. Do not add a test that merely searches prose.

- [ ] **Step 2: Document the exact safe workflow**

Document that the timer is not active until N100 operator installation after separate approval; list `--run` as the one manual command; state the actual-services exclusion, ConfigMap-to-relay result flow, and Telegram delivery expectation without secrets.

- [ ] **Step 3: Run integration verification**

Run: `python3 -m unittest tests.test_k8s_quarterly_sre_audit_automation tests.test_k8s_sre_telegram_tools tests.test_k8s_sre_telegram_manifests tests.test_documentation_index -v`

Expected: PASS.

- [ ] **Step 4: Run scope and static verification**

Generate a NUL-delimited Git name-status file from the branch base and run `python3 scripts/run_change_harness.py --input <file> --input-format git-name-status-z --agent-context`, then rerun it with the required `k8s-contracts=success` result. Run `git diff --check` and `bash -n` on the controller.
