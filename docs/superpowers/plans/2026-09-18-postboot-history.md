# Post-Boot Recovery History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Windows Supervisor가 시작될 때 비밀값 없는 부팅 관측 이벤트를 기존 recovery history에 기록해 post-boot 복구 이력을 구분 가능하게 함.

**Architecture:** `Start-Supervisor`가 lock 획득 뒤 `boot_observed` 이벤트를 한 번 기록하고 기존 post-boot recovery cycle을 그대로 실행함. 이벤트 timestamp는 기존 UTC 형식을 유지하며, 새 파일·새 예약 작업·새 Telegram 알림을 만들지 않음.

**Tech Stack:** Windows PowerShell, Python unittest, JSONL event log

**Spec:** `docs/superpowers/specs/2026-09-18-relay-postboot-observability-design.md`

## Global Constraints

- 변경 허용 파일은 `scripts/windows-bootstrap.ps1`, `scripts/verify_change_scope.py`, 직접 관련 테스트와 `docs/codex-work-loop.md`, `docs/public-uptime-monitor.md`로 한정함.
- 기존 recovery lock·3회 실패 제한·inactive K3s만 start·Portal 단일 rollout restart만 허용하는 정책을 유지함.
- active K3s 전체 restart, Secret·PVC·운영 데이터·Caddy·Tunnel ingress 변경을 금지함.
- Telegram 알림은 기존 Cloudflare Tunnel 장애·복구 전환만 유지하며 Windows/WSL/K3s 재부팅 알림을 추가하지 않음.

---

### Task 1: 부팅 관측 이벤트 계약 추가

**Files:**
- Modify: `tests/test_windows_bootstrap.py`
- Modify: `scripts/windows-bootstrap.ps1`

**Interfaces:**
- Consumes: `Start-Supervisor`, `Write-RecoveryEvent`
- Produces: `system/boot_observed/observed/windows_boot` JSONL event once per Supervisor start

- [ ] **Step 1: Write the failing test**

Add a test that extracts `Start-Supervisor` and asserts the exact event call occurs after supervisor lock acquisition and before the 120-second startup delay:

```python
self.assertIn(
    'Write-RecoveryEvent -Component "system" -Event "boot_observed" -Status "observed" -Action "windows_boot"',
    supervisor,
)
self.assertLess(
    supervisor.index('Write-RecoveryEvent -Component "system" -Event "boot_observed" -Status "observed" -Action "windows_boot"'),
    supervisor.index("Start-Sleep -Seconds $RecoveryStartupDelaySeconds"),
)
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_windows_bootstrap
```

Expected: FAIL because `boot_observed` is absent.

- [ ] **Step 3: Write the minimal implementation**

Immediately after `Start-Supervisor` obtains a non-null lock, insert exactly:

```powershell
Write-RecoveryEvent -Component "system" -Event "boot_observed" -Status "observed" -Action "windows_boot"
```

Do not change `Write-RecoveryEvent`, recovery state, scheduler registration, recovery action dispatch, or Telegram code.

- [ ] **Step 4: Run focused regression tests**

Run:

```bash
python3 -m unittest tests.test_windows_bootstrap tests.test_verify_change_scope tests.test_runtime_service_deployment_contract
git diff --check
```

Expected: all tests pass; recovery events remain bounded and secret-free.

- [ ] **Step 5: Commit the boot history change**

```bash
git add scripts/windows-bootstrap.ps1 tests/test_windows_bootstrap.py
git commit -m "feat: 부팅 관측 이력 기록 추가"
```

### Task 2: 운영 문서와 scope contract 정합성

**Files:**
- Modify: `docs/codex-work-loop.md`
- Modify: `docs/public-uptime-monitor.md`
- Test: `tests/test_windows_bootstrap.py`

**Interfaces:**
- Consumes: `boot_observed` JSONL event and existing 200-entry retention
- Produces: operator-visible event interpretation without new Telegram channel

`scripts/verify_change_scope.py` already classifies `scripts/windows-bootstrap.ps1` as a maintenance path, so it is not modified.

- [ ] **Step 1: Update only required operator documentation**

Document that `boot_observed` indicates Supervisor startup after a Windows boot-trigger path, uses the existing UTC timestamp, retains 200 events, and does not send Telegram. Do not add Event Log or journal raw-data retention guidance.

- [ ] **Step 2: Run relevant verification**

Run the focused test from Step 2, then:

```bash
python3 -m unittest tests.test_windows_bootstrap tests.test_verify_change_scope tests.test_runtime_service_deployment_contract
git diff --check
```

- [ ] **Step 3: Commit documentation updates**

```bash
git add docs/codex-work-loop.md docs/public-uptime-monitor.md
git commit -m "docs: 부팅 복구 이력 확인 기준 보강"
```

### Task 3: N100 post-boot evidence verification

**Files:**
- No repository file changes

**Interfaces:**
- Consumes: deployed `windows-bootstrap.ps1` and `recovery-events.jsonl`
- Produces: safe evidence that no prohibited recovery or notification path was introduced

- [ ] **Step 1: Review runtime boundaries before apply**

Read current recovery event history, Windows Task Scheduler state, K3s active state, and public health. Do not inspect credential values or trigger Windows/WSL/K3s restart.

- [ ] **Step 2: Apply only after explicit N100 operational approval**

Synchronize the approved repository change without recreating Portal, PVC, Caddy, Tunnel ingress, or existing K3s workloads.

- [ ] **Step 3: Verify static runtime contract**

Confirm the deployed script contains the exact `boot_observed` call and no new `Send-TunnelTelegramNotification` call outside existing Tunnel transition paths.

- [ ] **Step 4: Verify live safety**

Confirm KeepAlive task is running, K3s is active, Portal remains ready, no K3s restart occurred, and public health is HTTP 200 three times at 10-second intervals.

- [ ] **Step 5: Defer reboot-path runtime proof**

Do not reboot N100 to force an event. Verify `boot_observed` on the next approved maintenance reboot; until then report reboot-path runtime evidence as pending.
