# N100 자동 복구 감시 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Windows 부팅 복구 Daemon이 3분마다 N100 핵심 경로를 감시하고, 동일 장애가 2회 연속 발생할 때 필요한 구성요소만 제한적으로 복구하도록 함.

**Architecture:** `scripts/windows-bootstrap.ps1`에 읽기 전용 상태 점검, 실패 횟수 상태 파일, 단일 실행 잠금, 제한형 복구 함수를 추가함. 기존 `Start-PersonalServerStack`은 부팅·초기 복구 용도로만 유지하며, 정상 주기 점검에서는 호출하지 않음. 외부 장애·복구 알림은 기존 GitHub Actions Telegram 경로를 그대로 사용함.

**Tech Stack:** Windows PowerShell 5.1+, Windows Task Scheduler, WSL Ubuntu 24.04, systemd/K3s, Python `unittest`

**Spec:** `docs/superpowers/specs/2026-09-08-n100-auto-recovery-design.md`

## Global Constraints

- 감시 주기는 정확히 180초로 설정함.
- 같은 상태가 2회 연속 실패할 때만 복구함.
- 같은 항목의 자동 복구는 3회 연속 실패 후 중단함.
- Portal PVC, Kubernetes Secret, 운영 데이터, Caddyfile, Tunnel ingress, Compose Portal writer를 수정·재생성하지 않음.
- K3s가 active인 경우 K3s 전체 재시작을 수행하지 않음.
- 새 Telegram Secret·토큰·외부 알림 전송 기능을 만들지 않음.

---

### Task 1: 자동 복구 상태 계약 테스트

**Files:**
- Modify: `tests/test_windows_bootstrap.py`
- Modify: `scripts/windows-bootstrap.ps1`

**Interfaces:**
- Produces: `Get-RecoveryHealth`, `Register-RecoveryFailure`, `Reset-RecoveryFailure`, `Invoke-TargetedRecovery` PowerShell 함수
- Consumes: 기존 `Start-CloudflareTunnel`, `Invoke-WslCommand`, `Start-PersonalServerStack`

- [ ] **Step 1: 실패하는 테스트를 작성함**

```python
def test_daemon_uses_three_minute_health_interval_and_two_failures_before_recovery(self):
    self.assertIn("$RecoveryIntervalSeconds = 180", SCRIPT)
    self.assertIn("$RecoveryFailureThreshold = 2", SCRIPT)
    self.assertIn("if ($failureCount -lt $RecoveryFailureThreshold)", SCRIPT)

def test_targeted_recovery_does_not_recreate_compose_portal_writer(self):
    recovery = SCRIPT[SCRIPT.index("function Invoke-TargetedRecovery") : SCRIPT.index("function Start-Daemon")]
    self.assertNotIn("Start-PersonalServerStack", recovery)
    self.assertNotIn("docker compose", recovery)
```

- [ ] **Step 2: 테스트가 아직 실패함을 확인함**

Run: `python3 -m unittest tests.test_windows_bootstrap.WindowsBootstrapTests.test_daemon_uses_three_minute_health_interval_and_two_failures_before_recovery tests.test_windows_bootstrap.WindowsBootstrapTests.test_targeted_recovery_does_not_recreate_compose_portal_writer`

Expected: `FAIL` because the health interval, threshold, and target recovery function do not exist.

- [ ] **Step 3: 최소 상태 계약을 구현함**

```powershell
$RecoveryIntervalSeconds = 180
$RecoveryFailureThreshold = 2
$RecoveryMaxAttempts = 3

function Get-RecoveryHealth { <# return named component results #> }
function Register-RecoveryFailure([string]$Component) { <# persist only counters #> }
function Reset-RecoveryFailure([string]$Component) { <# clear successful component counter #> }
function Invoke-TargetedRecovery([string]$Component) { <# no Compose startup #> }
```

- [ ] **Step 4: 테스트 통과를 확인함**

Run: 동일한 `python3 -m unittest` 명령

Expected: `PASS`

### Task 2: 안전한 점검·복구 구현

**Files:**
- Modify: `scripts/windows-bootstrap.ps1`
- Modify: `tests/test_windows_bootstrap.py`

**Interfaces:**
- Consumes: Task 1의 상태 함수와 기존 `Start-CloudflareTunnel`
- Produces: `Invoke-RecoveryCycle` 및 `Start-Daemon`의 180초 주기 호출

- [ ] **Step 1: 실패하는 테스트를 작성함**

```python
def test_targeted_recovery_has_single_run_lock_and_component_limits(self):
    self.assertIn("New-Item -ItemType Directory -Path $RecoveryStateDirectory -Force", SCRIPT)
    self.assertIn("$RecoveryLockPath", SCRIPT)
    self.assertIn("$RecoveryMaxAttempts = 3", SCRIPT)
    self.assertIn("Start-CloudflareTunnel", SCRIPT)

def test_k3s_recovery_only_starts_inactive_service(self):
    recovery = SCRIPT[SCRIPT.index("function Invoke-TargetedRecovery") : SCRIPT.index("function Invoke-RecoveryCycle")]
    self.assertIn("systemctl is-active --quiet k3s", recovery)
    self.assertIn("systemctl start k3s", recovery)
    self.assertNotIn("systemctl restart k3s", recovery)
```

- [ ] **Step 2: 테스트가 아직 실패함을 확인함**

Run: `python3 -m unittest tests.test_windows_bootstrap.WindowsBootstrapTests.test_targeted_recovery_has_single_run_lock_and_component_limits tests.test_windows_bootstrap.WindowsBootstrapTests.test_k3s_recovery_only_starts_inactive_service`

Expected: `FAIL` because the lock and restricted K3s recovery code do not exist.

- [ ] **Step 3: 구성요소별 최소 복구를 구현함**

```powershell
switch ($Component) {
    "keepalive" { Start-ScheduledTask -TaskName "PersonalServer-WSL-KeepAlive" }
    "k3s" { & wsl.exe -d $WslDistribution -u root -- systemctl is-active --quiet k3s; if ($LASTEXITCODE -ne 0) { & wsl.exe -d $WslDistribution -u root -- systemctl start k3s } }
    "tunnel" { Start-CloudflareTunnel }
    "portal" { & wsl.exe -d $WslDistribution -u root -- k3s kubectl -n personal-server rollout restart deployment/portal-web }
}
```

Portal restart는 K3s API와 Portal Ready 상태가 2회 연속 실패했고, 해당 항목의 자동 복구 횟수가 제한 이내일 때만 허용함. NodePort 단독 실패는 Portal restart가 아닌 다음 점검 주기 재평가와 Tunnel 확인만 수행함.

- [ ] **Step 4: 테스트 통과를 확인함**

Run: 동일한 `python3 -m unittest` 명령

Expected: `PASS`

### Task 3: Daemon 연결 및 회귀 검증

**Files:**
- Modify: `scripts/windows-bootstrap.ps1`
- Modify: `tests/test_windows_bootstrap.py`

**Interfaces:**
- Consumes: `Invoke-RecoveryCycle`
- Produces: 부팅 대기 이후의 3분 주기 감시 루프

- [ ] **Step 1: 실패하는 테스트를 작성함**

```python
def test_daemon_runs_targeted_recovery_cycle_without_periodic_stack_recreation(self):
    daemon = SCRIPT[SCRIPT.index("function Start-Daemon") :]
    self.assertIn("Invoke-RecoveryCycle", daemon)
    self.assertIn("Start-Sleep -Seconds $RecoveryIntervalSeconds", daemon)
    self.assertNotIn("Start-PersonalServerStack", daemon)
```

- [ ] **Step 2: 테스트가 아직 실패함을 확인함**

Run: `python3 -m unittest tests.test_windows_bootstrap.WindowsBootstrapTests.test_daemon_runs_targeted_recovery_cycle_without_periodic_stack_recreation`

Expected: `FAIL` because the daemon still invokes full stack bootstrap in each loop.

- [ ] **Step 3: Daemon을 제한형 복구 주기에 연결함**

```powershell
Start-PersonalServerStack # startup only
while ($true) {
    try { Invoke-RecoveryCycle } catch { Write-Info "Recovery check failed: $($_.Exception.Message)" }
    Start-Sleep -Seconds $RecoveryIntervalSeconds
}
```

- [ ] **Step 4: 전체 관련 테스트·구문 검사를 수행함**

Run: `python3 -m unittest tests.test_windows_bootstrap tests.test_documentation_index`

Run: `pwsh -NoProfile -Command "[void][scriptblock]::Create((Get-Content -Raw scripts/windows-bootstrap.ps1))"` (사용 가능한 경우)

Expected: 모든 테스트 통과 및 PowerShell 구문 오류 없음.

- [ ] **Step 5: 변경 범위와 N100 정상 상태를 검증함**

Run: `git diff --check`

Run: `python3 scripts/run_change_harness.py --input <변경경로파일> --agent-context`

Run: N100에서 KeepAlive, `personal-server-autostart`, K3s Portal Ready, `https://len.pe.kr/health`를 읽기 전용으로 확인함.

Expected: 범위 위반 없음, 정상 서비스에 재시작을 유발하지 않음, 외부 health가 연속 `200`임.
