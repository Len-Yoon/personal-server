# 제한형 무인 자동복구 SRE Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** N100의 WSL·K3s 핵심 기반 장애를 서비스 단위로 복구하고, 정해진 실패 조건에서만 한 번의 Windows 긴급 재부팅으로 승격하며, GitHub Actions가 외부 상태 전환을 Telegram으로 알리도록 함.

**Architecture:** `personal-server-autostart`는 Windows에서 3분마다 로컬 상태를 확인하고, Tunnel은 WSL `cloudflared-personal-server.service` 한 개만 제어함. `keepalive` 또는 `k3s`의 제한 복구가 3회 실패한 경우에만 `PersonalServer-EmergencyReboot` 전용 SYSTEM 작업을 시작함. 외부 공개 정상 판정과 Telegram 전환 알림은 N100 자격 증명 없이 GitHub Actions가 계속 담당함.

**Tech Stack:** Windows PowerShell 5.1+, Windows Task Scheduler, WSL Ubuntu 24.04, systemd user service, K3s, GitHub Actions, Python `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-09-unattended-recovery-design.md`

## Global Constraints

- Portal PVC·Secret·운영 데이터·Caddyfile·Tunnel ingress·Compose Portal writer는 수정하거나 자동 재시작하지 않음.
- Tunnel·Portal·NodePort 단독 장애는 N100 긴급 재부팅 조건이 아님.
- 긴급 재부팅은 `keepalive` 또는 `k3s`가 3회 복구 실패하고, Windows 시작 뒤 20분이 지나며, 마지막 긴급 재부팅 뒤 6시간이 지난 경우에만 1회 허용함.
- 재부팅 상태 저장 실패, cooldown 계산 실패, 필수 작업 확인 실패 시 재부팅을 시작하지 않음.
- N100에는 Telegram Bot token이나 GitHub token을 저장하지 않음.
- 전용 재부팅 작업은 `shutdown.exe /r /f /t 60`만 실행하며, 운영자는 `shutdown /a`로 취소할 수 있어야 함.

---

## 파일 구조

| 파일 | 책임 |
|---|---|
| `scripts/windows-bootstrap.ps1` | WSL 사용자 Tunnel 서비스 감시·복구, 긴급 재부팅 전용 작업 등록·자격 판정·상태 저장 |
| `tests/test_windows_bootstrap.py` | PowerShell 계약의 안전 경계·상태 전이·예약 작업 제한 검증 |
| `docs/n100-mt4-setup.md` | 자동복구 계층, 긴급 재부팅 발동·취소·재부팅 뒤 확인 절차 |
| `docs/cloudflare-tunnel.md` | Tunnel 서비스 단일 소유자와 자동복구 훈련 절차 |
| `docs/recovery-drill.md` | 승인된 Tunnel·기반 장애 훈련의 중단·롤백 기준 |
| `tests/test_documentation_index.py` | 최신 운영 문서의 자동복구·훈련 경계 계약 |

## Task 1: Tunnel 사용자 서비스 계약을 테스트로 고정

**Files:**
- Modify: `tests/test_windows_bootstrap.py`
- Modify: `scripts/windows-bootstrap.ps1`

**Interfaces:**
- Consumes: `$WslDistribution = "Ubuntu-24.04"`, Windows 사용자 `window`, 기존 `Invoke-WslWithTimeout`.
- Produces: `Test-CloudflareTunnelService`, `Start-CloudflareTunnel` PowerShell 함수.

- [ ] **Step 1: 실패하는 Tunnel 서비스 계약 테스트를 추가**

```python
def test_tunnel_recovery_controls_the_registered_wsl_user_service(self):
    recovery = SCRIPT[
        SCRIPT.index("function Test-CloudflareTunnelService")
        : SCRIPT.index("function Update-HostMetrics")
    ]

    self.assertIn('$WslServiceUser = "window"', SCRIPT)
    self.assertIn('"systemctl", "--user", "is-active", "--quiet", "cloudflared-personal-server.service"', recovery)
    self.assertIn('"systemctl", "--user", "start", "cloudflared-personal-server.service"', recovery)
    self.assertNotIn("Start-Process -FilePath 'wsl.exe'", recovery)
    self.assertNotIn("'cloudflared', 'tunnel', 'run'", recovery)
```

- [ ] **Step 2: 새 테스트가 현재 코드에서 실패하는지 확인**

Run:

```bash
python3 -m unittest tests.test_windows_bootstrap.WindowsBootstrapTests.test_tunnel_recovery_controls_the_registered_wsl_user_service -v
```

Expected: `Test-CloudflareTunnelService` 또는 WSL 사용자 서비스 명령이 없어 실패함.

- [ ] **Step 3: 최소 PowerShell 구현 추가**

`scripts/windows-bootstrap.ps1`에서 프로세스 전용 Tunnel 시작 함수를 다음 서비스 제어 계약으로 대체함.

```powershell
$WslServiceUser = "window"
$CloudflareTunnelService = "cloudflared-personal-server.service"

function Test-CloudflareTunnelService {
    return (Invoke-WslWithTimeout -Arguments @(
        "-d", $WslDistribution, "-u", $WslServiceUser, "--",
        "bash", "-lc", "systemctl --user is-active --quiet $CloudflareTunnelService"
    ) -Operation "Cloudflare Tunnel service probe")
}

function Start-CloudflareTunnel {
    if (Test-CloudflareTunnelService) { return $false }
    if (-not (Invoke-WslWithTimeout -Arguments @(
        "-d", $WslDistribution, "-u", $WslServiceUser, "--",
        "bash", "-lc", "systemctl --user start $CloudflareTunnelService"
    ) -Operation "Cloudflare Tunnel service start")) { return $false }
    return (Test-CloudflareTunnelService)
}
```

`Get-RecoveryHealth`의 `tunnel`은 서비스 active와 기존 연결 프로세스가 모두 확인될 때만 `healthy`로 설정함.

- [ ] **Step 4: Tunnel 계약 테스트 통과 확인**

Run:

```bash
python3 -m unittest tests.test_windows_bootstrap.WindowsBootstrapTests.test_tunnel_recovery_controls_the_registered_wsl_user_service tests.test_windows_bootstrap.WindowsBootstrapTests.test_recovery_health_checks_all_five_components_without_stack_bootstrap -v
```

Expected: PASS. Portal·K3s·NodePort 경계는 유지됨.

- [ ] **Step 5: 커밋**

```bash
git add scripts/windows-bootstrap.ps1 tests/test_windows_bootstrap.py
git commit -m "fix: tunnel 복구를 사용자 서비스로 통일"
```

## Task 2: 긴급 재부팅 전용 작업과 자격 판정을 추가

**Files:**
- Modify: `tests/test_windows_bootstrap.py`
- Modify: `scripts/windows-bootstrap.ps1`

**Interfaces:**
- Consumes: `$RecoveryAttemptCounts`, `$RecoveryStatePath`, `Invoke-RecoveryCycle`.
- Produces: `Install-EmergencyRebootTask`, `Test-EmergencyRebootEligible`, `Request-EmergencyReboot`.

- [ ] **Step 1: 실패하는 긴급 재부팅 안전 계약 테스트를 추가**

```python
def test_emergency_reboot_is_limited_to_exhausted_keepalive_or_k3s_recovery(self):
    reboot = SCRIPT[
        SCRIPT.index("function Install-EmergencyRebootTask")
        : SCRIPT.index("function Set-RecoveryTaskSettings")
    ]

    self.assertIn('$EmergencyRebootTaskName = "PersonalServer-EmergencyReboot"', SCRIPT)
    self.assertIn('$EmergencyRebootGraceSeconds = 1200', SCRIPT)
    self.assertIn('$EmergencyRebootCooldownSeconds = 21600', SCRIPT)
    self.assertIn('"keepalive", "k3s"', reboot)
    self.assertNotIn('"tunnel", "portal", "nodeport"', reboot)
    self.assertIn('shutdown.exe /r /f /t 60', reboot)
    self.assertIn('/RU "SYSTEM"', reboot)
    self.assertIn('/RL HIGHEST', reboot)
```

- [ ] **Step 2: 새 테스트가 현재 코드에서 실패하는지 확인**

Run:

```bash
python3 -m unittest tests.test_windows_bootstrap.WindowsBootstrapTests.test_emergency_reboot_is_limited_to_exhausted_keepalive_or_k3s_recovery -v
```

Expected: 긴급 재부팅 작업과 안전 상수가 없어 실패함.

- [ ] **Step 3: 재부팅 전용 작업·상태·자격 판정 구현**

`scripts/windows-bootstrap.ps1`에 다음 고정값과 함수 계약을 추가함.

```powershell
$EmergencyRebootTaskName = "PersonalServer-EmergencyReboot"
$EmergencyRebootGraceSeconds = 1200
$EmergencyRebootCooldownSeconds = 21600

function Install-EmergencyRebootTask {
    # <Triggers> 없이 SYSTEM ServiceAccount XML을 등록하여
    # Start-ScheduledTask로만 실행할 수 있게 함.
    $result = & schtasks.exe /Create /TN $EmergencyRebootTaskName /XML $temporaryTaskXml /F 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "Failed to register emergency reboot task." }
}
```

`Save-RecoveryFailureState`와 `Load-RecoveryFailureState`에 `last_emergency_reboot_at` UTC ISO 8601 원본과 원인 구성요소를 추가함. `Test-EmergencyRebootEligible([string]$Component)`는 `keepalive`·`k3s`만 허용하고, 3회 시도 소진·20분 부팅 유예·6시간 cooldown·정상 상태 저장을 모두 검증함. `Request-EmergencyReboot`는 상태를 먼저 저장한 뒤 `Start-ScheduledTask -TaskName $EmergencyRebootTaskName`만 호출함.

- [ ] **Step 4: 최종 승격 연결**

`Invoke-TargetedRecovery`가 실패한 뒤에만 `Test-EmergencyRebootEligible $component`를 호출하고 true일 때만 `Request-EmergencyReboot $component`를 호출함. `tunnel`, `portal`, `nodeport` 분기에는 이 호출을 추가하지 않음. 재부팅 요청 뒤 현재 cycle의 추가 복구를 중단함.

- [ ] **Step 5: 긴급 재부팅 계약 테스트 통과 확인**

Run:

```bash
python3 -m unittest tests.test_windows_bootstrap.WindowsBootstrapTests.test_emergency_reboot_is_limited_to_exhausted_keepalive_or_k3s_recovery tests.test_windows_bootstrap.WindowsBootstrapTests.test_recovery_attempt_budget_is_separate_from_health_failures_and_stops_after_three_attempts tests.test_windows_bootstrap.WindowsBootstrapTests.test_recovery_cycle_skips_the_fourth_action_before_invoking_targeted_recovery -v
```

Expected: PASS. 네 번째 서비스 복구와 Tunnel·Portal·NodePort 기반 재부팅은 없어야 함.

- [ ] **Step 6: 커밋**

```bash
git add scripts/windows-bootstrap.ps1 tests/test_windows_bootstrap.py
git commit -m "feat: 핵심 장애 긴급 재부팅 보호 추가"
```

## Task 3: 운영 문서와 실제 훈련·롤백 절차 반영

**Files:**
- Modify: `docs/n100-mt4-setup.md`
- Modify: `docs/cloudflare-tunnel.md`
- Modify: `docs/recovery-drill.md`
- Modify: `tests/test_documentation_index.py`

**Interfaces:**
- Consumes: `PersonalServer-EmergencyReboot`, 20분 유예, 6시간 cooldown, `shutdown /a` 취소 계약.
- Produces: 운영자용 발동 기준·취소·수동 rollback·Tunnel 훈련 절차.

- [ ] **Step 1: 실패하는 문서 계약 테스트를 추가**

```python
def test_n100_docs_document_reboot_limits_and_tunnel_service_recovery(self):
    n100 = Path("docs/n100-mt4-setup.md").read_text(encoding="utf-8")
    tunnel = Path("docs/cloudflare-tunnel.md").read_text(encoding="utf-8")
    drill = Path("docs/recovery-drill.md").read_text(encoding="utf-8")

    self.assertIn("PersonalServer-EmergencyReboot", n100)
    self.assertIn("20분", n100)
    self.assertIn("6시간", n100)
    self.assertIn("shutdown /a", n100)
    self.assertIn("Tunnel·Portal·NodePort 단독 장애", n100)
    self.assertIn("cloudflared-personal-server.service", tunnel)
    self.assertIn("Tunnel만", drill)
```

- [ ] **Step 2: 새 문서 테스트가 현재 문서에서 실패하는지 확인**

Run:

```bash
python3 -m unittest tests.test_documentation_index.DocumentationIndexTests.test_n100_docs_document_reboot_limits_and_tunnel_service_recovery -v
```

Expected: 긴급 재부팅·Tunnel 서비스 복구·훈련 문구가 없어 실패함.

- [ ] **Step 3: 운영 문서 수정**

`docs/n100-mt4-setup.md`에 재부팅 발동 조건, 금지 대상, 60초 취소 명령, 재부팅 뒤 상태 확인을 추가함. `docs/cloudflare-tunnel.md`에는 `systemctl --user start cloudflared-personal-server.service`를 유일한 Tunnel 수동 rollback으로 명시함. `docs/recovery-drill.md`에는 Tunnel만 중지 → GitHub monitor 장애 확인 → 자동복구 대기 → 외부 health·복구 Telegram 확인 → 실패 시 사용자 서비스 수동 시작 순서를 추가함.

- [ ] **Step 4: 문서 계약 테스트 통과 확인**

Run:

```bash
python3 -m unittest tests.test_documentation_index -v
```

Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add docs/n100-mt4-setup.md docs/cloudflare-tunnel.md docs/recovery-drill.md tests/test_documentation_index.py
git commit -m "docs: 무인 복구와 긴급 재부팅 훈련 정리"
```

## Task 4: 통합 검증·독립 운영 검토·N100 적용

**Files:**
- Modify: 없음
- Verify: `scripts/windows-bootstrap.ps1`, 관련 테스트, 운영 문서, GitHub Actions public uptime monitor.

**Interfaces:**
- Consumes: Task 1~3의 PowerShell·문서 계약.
- Produces: PR 검증 증적, N100 적용 결과, 안전한 훈련 결과.

- [ ] **Step 1: 전체 관련 테스트와 정적 검사를 실행**

Run:

```bash
python3 -m unittest tests.test_windows_bootstrap tests.test_public_uptime_monitor tests.test_documentation_index tests.test_verify_change_scope tests.test_change_harness -v
git diff --check
```

Expected: PASS. 실패 시 한 원인만 최소 수정하고 같은 검증은 최대 3회 수행함.

- [ ] **Step 2: 변경 범위 하네스 실행**

Run:

```bash
git diff --name-status -z --find-renames origin/main HEAD > /tmp/unattended-recovery.paths
python3 scripts/run_change_harness.py --input /tmp/unattended-recovery.paths --input-format git-name-status-z --agent-context
```

Expected: `maintenance` 검증 요구를 확인함. 관련 테스트 통과 뒤 `--check-result maintenance=success`를 전달해 `ready_for_review`를 확인함.

- [ ] **Step 3: 독립 운영 검토 수행**

검토자는 SYSTEM 작업의 명령이 정확히 `shutdown.exe /r /f /t 60` 하나인지, keepalive·k3s 외 구성요소가 재부팅 조건에 없는지, 유예·cooldown·상태 저장 실패가 fail-closed인지, Secret·PVC·Caddy·Tunnel ingress가 변경·출력되지 않는지, WSL user systemd가 `window` 사용자에서만 실행되는지를 확인함.

- [ ] **Step 4: PR 생성과 CI 확인**

```bash
git push -u origin codex/unattended-recovery-sre
gh pr create --base main --head codex/unattended-recovery-sre --title "feat: 제한형 무인 자동복구 SRE 강화"
gh pr checks --watch
```

Expected: CI와 리뷰 통과 뒤 사용자 병합 승인을 대기함.

- [ ] **Step 5: N100 적용 전 사용자 승인 확인**

`PersonalServer-EmergencyReboot` 작업 생성은 최고 권한 운영 변경임. PR 병합과 N100 적용은 별도 사용자 승인 뒤에만 실행함.

- [ ] **Step 6: N100 적용 뒤 비파괴 확인**

```powershell
Get-ScheduledTask -TaskName personal-server-autostart,PersonalServer-EmergencyReboot
wsl -d Ubuntu-24.04 -u window -- bash -lc 'systemctl --user is-active cloudflared-personal-server.service'
```

Expected: 감시 작업과 전용 재부팅 작업이 존재하고 Tunnel 서비스는 `active`임. 실제 긴급 재부팅은 이 단계에서 실행하지 않음.

- [ ] **Step 7: 승인된 Tunnel 훈련 수행**

Tunnel만 중지하고 자동복구·외부 health·장애/복구 Telegram 전환을 확인함. 정의된 시간 안에 복구되지 않으면 아래 명령으로 즉시 rollback함.

```powershell
wsl -d Ubuntu-24.04 -u window -- bash -lc 'systemctl --user start cloudflared-personal-server.service'
```

Expected: Portal·Caddy·Secret·PVC·Tunnel ingress 변경 없이 외부 health가 정상화됨.
