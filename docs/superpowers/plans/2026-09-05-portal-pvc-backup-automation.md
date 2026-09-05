# Portal PVC Backup Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** N100에서 안전한 암호화 credential로 Portal PVC 백업을 하루 한 번 실행하고 Telegram 결과를 보고함.

**Architecture:** systemd user timer가 기존 backup verifier만 호출함. encrypted rclone config과 passphrase는 host-bound credential로만 보관하고 rclone password-command로만 전달함. runner는 고정 ConfigMap에 안전한 결과만 기록하고 relay가 이를 한글 Telegram으로 전송함.

**Tech Stack:** Bash, Python 3.11 stdlib, systemd 255 credentials/timers, K3s, Kubernetes Secret, Telegram relay.

**Spec:** `docs/superpowers/specs/2026-09-05-portal-pvc-backup-automation-design.md`

## Global Constraints

- CronJob은 허용되지만 rclone secret 복제를 피하기 위해 사용하지 않음.
- sudo password, rclone config/passphrase, Telegram token, Alertmanager token은 출력·커밋·평문 저장·환경 변수 전달 금지.
- 자동 실행은 `portal-pvc-backup-verify.sh --go`만 호출함.
- 모든 시각은 내부적으로 UTC ISO 8601을 사용함.

### Task 1: Relay backup status contract

**Files:**
- Modify: `sre-telegram-relay/app/main.py`
- Modify: `tests/test_sre_telegram_relay.py`
- Modify: `infra/k8s/sre-telegram/base.yaml`
- Modify: `tests/test_k8s_sre_telegram_manifests.py`

- [ ] Add failing tests for malformed/duplicate backup ConfigMap data and Korean messages for `completed`, `unchanged`, `failed`, `restore_failed`.
- [ ] Implement a ConfigMap-backed report reader with a fixed allowlist of status, run ID, UTC timestamp and safe stage fields; use bounded best-effort dedup while preserving at-least-once delivery.
- [ ] Grant relay only `get` access to the one backup status ConfigMap; retain ClusterIP and existing Secret mounts unchanged.
- [ ] Run focused relay/manifest tests, `python3 -m py_compile`, and manifest syntax checks.

### Task 2: Backup runner and credential enrollment

**Files:**
- Create: `infra/k8s/tools/portal-pvc-backup-automation.sh`
- Create: `infra/k8s/backup-automation/personal-server-portal-pvc-backup.service.tmpl`
- Create: `infra/k8s/backup-automation/personal-server-portal-pvc-backup.timer.tmpl`
- Modify: `tests/test_k8s_portal_pvc_backup_verify.py`
- Create: `tests/test_k8s_portal_pvc_backup_automation.py`

- [ ] Add failing tests for private credential setup contract, timer schedule, one-shot result classification, ConfigMap schema and cleanup behavior.
- [ ] Implement `--preflight`, `--enroll`, `--install`, `--run`, `--status`, `--uninstall`; enrollment reads rclone password without echo and creates encrypted credentials without plaintext files.
- [ ] Encrypt rclone config and passphrase with `systemd-creds`; pass only file paths into the backup tool and use rclone `--password-command` rather than a secret environment variable.
- [ ] Run focused tests and shell/systemd static checks.

### Task 3: Operator docs and live deployment

**Files:**
- Modify: `infra/k8s/README.md`
- Modify: `AGENTS.md`
- Create: `docs/superpowers/plans/2026-09-05-portal-pvc-backup-automation.md`

- [ ] Document only command names and safety expectations; never document concrete secret values.
- [ ] Run the change harness with changed-path input and focused verification results.
- [ ] Build/import the relay image, apply relay manifest, enroll credentials through a local masked prompt, install and start the timer.
- [ ] Run a one-shot live backup, verify Portal health, relay health, and Telegram delivery; then check timer is enabled.

### Task 4: Independent security and operational review

**Files:**
- Review: all changed files

- [ ] Verify diff has no secret value, broad ClusterRole, public Service, plaintext credential file, or second scheduler.
- [ ] Verify rollback disables timer before deleting encrypted credentials/status ConfigMap and leaves Portal running.
- [ ] Run focused regression suite and `git diff --check`.
- [ ] Create PR only after review results are clean.
