# P2 재시작 후 운영 점검 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Windows 재시작 뒤 기존 Supervisor가 WSL·K3s·Portal·외부 health 상태를 안전하게 1회 확인하고 기존 자동복구 한계를 유지하게 함.

**Architecture:** 기존 `Start-Supervisor`의 초기 대기와 Daemon 실행 사이에 초기 recovery cycle을 삽입함. 별도 스케줄러·상태 저장소를 만들지 않고 기존 recovery lock, 구성요소별 failure/attempt state, Tunnel 전환 알림을 재사용함. 결과는 기존 비밀값 없는 event log와 운영 문서에만 반영함.

**Tech Stack:** Windows PowerShell, WSL2, K3s, Python `unittest`, Markdown.

**Spec:** `docs/superpowers/specs/2026-09-14-p2-postboot-recovery-design.md`

## Global Constraints

- `scripts/windows-bootstrap.ps1`만 자동복구 런타임 변경 파일로 사용함.
- active K3s 전체 restart를 수행하지 않음.
- 허용 복구는 KeepAlive 시작, inactive K3s 시작, 제한된 `portal-web` rollout restart, Cloudflare Tunnel 재기동으로 한정함.
- Telegram은 Cloudflare Tunnel 장애·복구 전환에만 사용하며 Secret·token·Chat ID를 출력·저장하지 않음.
- 외부 health 확인은 `https://len.pe.kr/health`를 10초 간격으로 3회 호출해 모두 HTTP 200이어야 함.
- N100 적용은 PR CI·독립 검토·사용자 병합 승인 뒤에만 수행함.

---

### Task 1: 초기 Supervisor 점검과 외부 health 3회 검증

**Files:**
- Modify: `scripts/windows-bootstrap.ps1`
- Test: `tests/test_windows_bootstrap.py`

**Interfaces:**
- Consumes: `Start-Supervisor`, `Invoke-RecoveryCycle`, `Test-PublicPortalHealth`, `Write-RecoveryEvent`
- Produces: `Invoke-PostBootRecoveryCheck` 함수와 `Test-PublicPortalHealthThreeTimes` 함수

- [ ] **Step 1: 실패하는 정적 계약 테스트 작성**

```python
def test_supervisor_runs_one_post_boot_check_before_starting_daemon(self):
    supervisor = SCRIPT[SCRIPT.index("function Start-Supervisor") : SCRIPT.index("function Enter-DaemonLock")]
    self.assertIn("Invoke-PostBootRecoveryCheck", supervisor)
    self.assertLess(
        supervisor.index("Invoke-PostBootRecoveryCheck"),
        supervisor.index("Start-Process"),
    )

def test_post_boot_public_health_requires_three_checks(self):
    self.assertIn("function Test-PublicPortalHealthThreeTimes", SCRIPT)
    self.assertIn("for ($attempt = 1; $attempt -le 3; $attempt++)", SCRIPT)
    self.assertIn("Start-Sleep -Seconds 10", SCRIPT)
```

- [ ] **Step 2: 실패를 확인**

Run: `python3 -m unittest tests.test_windows_bootstrap.WindowsBootstrapTests.test_supervisor_runs_one_post_boot_check_before_starting_daemon tests.test_windows_bootstrap.WindowsBootstrapTests.test_post_boot_public_health_requires_three_checks -v`

Expected: 새 함수·호출이 없어 assertion failure 발생.

- [ ] **Step 3: 최소 구현 작성**

```powershell
function Test-PublicPortalHealthThreeTimes {
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        if (-not (Test-PublicPortalHealth)) { return $false }
        if ($attempt -lt 3) { Start-Sleep -Seconds 10 }
    }
    return $true
}

function Invoke-PostBootRecoveryCheck {
    Write-RecoveryEvent -Component "system" -Event "post_boot_check" -Status "started" -Action "none"
    try {
        Invoke-RecoveryCycle
        if (-not (Test-PublicPortalHealthThreeTimes)) { throw "Public Portal health did not pass three checks." }
        Write-RecoveryEvent -Component "system" -Event "post_boot_check" -Status "passed" -Action "none"
    } catch {
        Write-RecoveryEvent -Component "system" -Event "post_boot_check" -Status "failed" -Action "none"
    }
}
```

Call `Invoke-PostBootRecoveryCheck` exactly once in `Start-Supervisor` after the existing boot delay and before the Daemon `Start-Process` loop. Preserve Daemon startup even if the post-boot check fails.

- [ ] **Step 4: focused tests 실행**

Run: `python3 -m unittest tests.test_windows_bootstrap -v`

Expected: 모든 Windows bootstrap 계약 테스트 통과.

- [ ] **Step 5: 금지 동작 회귀 확인**

Run: `rg -n 'restart.*k3s|delete.*persistentvolumeclaim|kubectl.*secret|Caddyfile' scripts/windows-bootstrap.ps1`

Expected: 새 금지 동작 추가 없음. 기존 허용된 `portal-web rollout restart`와 Tunnel 재시작 외의 복구 동작 없음.

### Task 2: 운영 문서와 로드맵 최신화

**Files:**
- Modify: `docs/operations-roadmap.md`
- Modify: `docs/public-uptime-monitor.md`
- Modify: `docs/codex-work-loop.md`
- Test: `tests/test_documentation_index.py`

**Interfaces:**
- Consumes: Task 1의 `post_boot_check` event 이름과 기존 recovery event log 형식
- Produces: 완료 상태가 반영된 로드맵과 재시작 뒤 점검 절차

- [ ] **Step 1: 실패하는 문서 계약 테스트 작성**

```python
def test_reboot_docs_describe_post_boot_check_without_new_telegram_message(self):
    uptime = (ROOT / "docs/public-uptime-monitor.md").read_text(encoding="utf-8")
    self.assertIn("post_boot_check", uptime)
    self.assertIn("Cloudflare Tunnel 장애·복구 전환", uptime)
```

- [ ] **Step 2: 실패를 확인**

Run: `python3 -m unittest tests.test_documentation_index.DocumentationIndexTests.test_reboot_docs_describe_post_boot_check_without_new_telegram_message -v`

Expected: `post_boot_check` 문서화가 없어 assertion failure 발생.

- [ ] **Step 3: 최소 문서 수정**

- `operations-roadmap.md`에서 실제 완료된 분기 SRE 감사, 백업 CronJob, 공개 감시 범위, Telegram relay 시험을 완료로 변경함.
- 남은 P2를 월간 훈련 일정·담당자·보관 위치, 작업 증적 정리, 분기 문서 대조로 한정함.
- `public-uptime-monitor.md`에 재시작 뒤 `post_boot_check` 기록·WSL/K3s/Portal/external health 3회 확인과 신규 Telegram 메시지 미발송을 명시함.
- `codex-work-loop.md`에 N100 자동복구 변경의 적용 전후 외부 health 3회 검증과 event log 확인을 추가함.

- [ ] **Step 4: 문서 계약 테스트 실행**

Run: `python3 -m unittest tests.test_documentation_index -v`

Expected: 문서 계약 전체 통과.

### Task 3: 범위·통합 검증과 독립 검토

**Files:**
- Modify: `scripts/verify_change_scope.py` (필요한 경우에만; `windows-bootstrap.ps1` maintenance 분류 유지 검증 목적)
- Test: `tests/test_change_harness.py`, `tests/test_verify_change_scope.py`

**Interfaces:**
- Consumes: 변경 경로의 Git name-status Z 목록
- Produces: `maintenance=success`가 필요한 자동복구 변경 범위 증적

- [ ] **Step 1: 변경 경로 하네스 실행**

```bash
git diff --name-status -z --find-renames origin/main HEAD > /tmp/p2-postboot-paths.z
python3 scripts/run_change_harness.py --input /tmp/p2-postboot-paths.z \
  --input-format git-name-status-z --agent-context
```

- [ ] **Step 2: 관련 검사 결과 반영**

```bash
python3 scripts/run_change_harness.py --input /tmp/p2-postboot-paths.z \
  --input-format git-name-status-z --check-result maintenance=success --agent-context
```

- [ ] **Step 3: 정적·회귀 검사 실행**

Run: `python3 -m unittest tests.test_windows_bootstrap tests.test_documentation_index tests.test_change_harness tests.test_verify_change_scope -v`

Expected: 전체 통과.

- [ ] **Step 4: 독립 운영 검토**

확인 항목: 3회 제한 유지, active K3s restart 부재, 비밀값 미출력, 신규 Telegram 경로 부재, 외부 health 3회 검증, 문서와 구현 event 이름 일치.

- [ ] **Step 5: 사용자 승인 뒤 N100 적용**

PR CI·독립 검토·사용자 병합 승인 뒤에만 N100에서 최신 commit, 이미지 불필요 여부, Windows 예약 작업 상태, event log와 외부 health 3회를 확인함. 변경 명령 응답이 불완전하면 재실행 전에 읽기 전용 상태를 확인함.
