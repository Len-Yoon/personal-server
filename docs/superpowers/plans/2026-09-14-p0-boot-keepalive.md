# P0 무로그인 KeepAlive 부팅 복구 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Windows 로그인 없이 부팅된 N100에서 WSL KeepAlive가 시작되어 Supervisor 초기 점검이 정상 복구 경로를 수행하도록 함.

**Architecture:** scripts/windows-bootstrap.ps1의 설치 경로가 기존 KeepAlive 예약 작업을 Password 방식의 BootTrigger 작업으로 재등록함. KeepAlive 명령은 기존 WSL root 무기한 sleep을 유지하고, Supervisor·K3s·Portal·Tunnel의 복구 정책과 비밀값 경계는 변경하지 않음.

**Tech Stack:** Windows PowerShell, schtasks.exe, Windows Task Scheduler, Python unittest, K3s, WSL

**Spec:** docs/superpowers/specs/2026-09-14-p0-boot-keepalive-design.md

## Global Constraints

- KeepAlive는 PersonalServer-WSL-KeepAlive, /SC ONSTART, 현재 Windows 사용자, /RP *를 사용함.
- KeepAlive 실행 명령은 기존 Ubuntu-24.04 root WSL 무기한 sleep을 유지함.
- Windows 계정 암호·Telegram 값·Kubernetes Secret·PVC·운영 데이터는 생성·출력·저장하지 않음.
- K3s active 상태 전체 재시작, Portal PVC·Secret·Caddyfile·Cloudflare Tunnel ingress 수정은 금지함.
- 실제 N100 적용은 PR 병합과 사용자 승인 뒤에만 진행하며, Task Scheduler 암호는 운영자가 직접 입력함.

---

### Task 1: KeepAlive 등록 계약 테스트

**Files:**
- Modify: tests/test_windows_bootstrap.py

**Interfaces:**
- Consumes: scripts/windows-bootstrap.ps1의 Install-ScheduledTask와 새 Install-KeepAliveTask
- Produces: 무로그인 KeepAlive Task Scheduler 계약을 고정하는 회귀 테스트

- [ ] **Step 1: 실패하는 계약 테스트 작성**

~~~python
def test_install_task_registers_keepalive_at_boot_without_interactive_token(self):
    installer = SCRIPT[
        SCRIPT.index("function Install-KeepAliveTask")
        : SCRIPT.index("function Set-RecoveryTaskSettings")
    ]
    self.assertIn('$KeepAliveTaskName = "PersonalServer-WSL-KeepAlive"', SCRIPT)
    self.assertIn("/SC ONSTART", installer)
    self.assertIn("/RP *", installer)
    self.assertIn('"while true; do sleep 3600; done"', installer)
    self.assertNotIn("/SC ONLOGON", installer)
    self.assertNotIn("InteractiveToken", installer)
~~~

- [ ] **Step 2: 실패를 확인**

Run: python3 -m unittest tests.test_windows_bootstrap.WindowsBootstrapTests.test_install_task_registers_keepalive_at_boot_without_interactive_token -v

Expected: Install-KeepAliveTask가 없어 assertion 또는 문자열 탐색 실패.

- [ ] **Step 3: 설치 흐름 계약 테스트 추가**

~~~python
def test_install_task_registers_keepalive_before_supervisor(self):
    installer = SCRIPT[
        SCRIPT.index("function Install-ScheduledTask")
        : SCRIPT.index("\nLoad-RecoveryFailureState")
    ]
    self.assertIn("Install-KeepAliveTask", installer)
    self.assertLess(installer.index("Install-KeepAliveTask"), installer.index("$taskAction ="))
~~~

- [ ] **Step 4: 테스트를 커밋**

~~~bash
git add tests/test_windows_bootstrap.py
git commit -m "test: KeepAlive 부팅 작업 계약 추가"
~~~

### Task 2: Password BootTrigger KeepAlive 등록 구현

**Files:**
- Modify: scripts/windows-bootstrap.ps1
- Test: tests/test_windows_bootstrap.py

**Interfaces:**
- Consumes: WslDistribution, TaskName, schtasks.exe 등록 방식
- Produces: Install-KeepAliveTask와 Install-ScheduledTask의 KeepAlive 등록 호출

- [ ] **Step 1: 최소 구성 상수와 등록 함수 추가**

~~~powershell
$KeepAliveTaskName = "PersonalServer-WSL-KeepAlive"
$KeepAliveTaskAction = 'wsl.exe -d Ubuntu-24.04 -u root --exec /bin/bash -lc "while true; do sleep 3600; done"'

function Install-KeepAliveTask([string]$RunAsUser) {
    $createOutput = (& schtasks.exe /Create /TN $KeepAliveTaskName /SC ONSTART /RU $RunAsUser /RP * /TR $KeepAliveTaskAction /RL LIMITED /F 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to register scheduled task '$KeepAliveTaskName'."
    }
}
~~~

- [ ] **Step 2: Supervisor 설치 전 KeepAlive 등록 호출**

~~~powershell
$runAsUser = "$env:USERDOMAIN\$env:USERNAME"
Install-KeepAliveTask -RunAsUser $runAsUser
$taskAction = "powershell.exe ... windows-bootstrap.ps1 -Supervisor"
~~~

- [ ] **Step 3: 관련 회귀 테스트 실행**

Run: python3 -m unittest tests.test_windows_bootstrap -v

Expected: 전체 통과.

- [ ] **Step 4: 구현을 커밋**

~~~bash
git add scripts/windows-bootstrap.ps1 tests/test_windows_bootstrap.py
git commit -m "fix: KeepAlive 부팅 작업으로 전환"
~~~

### Task 3: 운영 문서와 적용 검증 갱신

**Files:**
- Modify: docs/n100-mt4-setup.md
- Modify: docs/public-uptime-monitor.md
- Modify: tests/test_documentation_index.py

**Interfaces:**
- Consumes: KeepAlive BootTrigger와 Password LogonType 계약
- Produces: 암호 직접 입력·무로그인 재부팅 검증·외부 health 3회 확인 절차

- [ ] **Step 1: 실패하는 문서 계약 테스트 작성**

~~~python
def test_reboot_docs_require_boot_trigger_keepalive_and_password_prompt(self):
    mt4 = (ROOT / "docs" / "n100-mt4-setup.md").read_text(encoding="utf-8")
    self.assertIn("BootTrigger", mt4)
    self.assertIn("Password", mt4)
    self.assertIn("로그인 없이", mt4)
~~~

- [ ] **Step 2: 실패를 확인**

Run: python3 -m unittest tests.test_documentation_index.DocumentationIndexTests.test_reboot_docs_require_boot_trigger_keepalive_and_password_prompt -v

Expected: 기존 문서가 로그인 뒤 KeepAlive를 설명하므로 assertion failure.

- [ ] **Step 3: 문서 최소 수정**

n100-mt4-setup.md에 InstallTask 실행 시 KeepAlive와 Supervisor 모두 Password 방식 Task Scheduler 암호 입력이 필요함을 명시함. 재부팅 검증은 로그인 없이 KeepAlive Running, post_boot_check passed, 외부 health 3회 HTTP 200을 순서대로 확인하도록 갱신함.

public-uptime-monitor.md의 로그인 의존 설명을 KeepAlive BootTrigger 조건과 무로그인 검증 기준으로 갱신함. Telegram의 Tunnel 전환 전용 정책은 유지함.

- [ ] **Step 4: 문서 관련 테스트 실행**

Run: python3 -m unittest tests.test_documentation_index -v

Expected: 전체 통과.

- [ ] **Step 5: 문서를 커밋**

~~~bash
git add docs/n100-mt4-setup.md docs/public-uptime-monitor.md tests/test_documentation_index.py
git commit -m "docs: 무로그인 KeepAlive 검증 절차 갱신"
~~~

### Task 4: 통합 검증과 독립 운영 검토

**Files:**
- Verify: scripts/windows-bootstrap.ps1
- Verify: tests/test_windows_bootstrap.py
- Verify: tests/test_documentation_index.py
- Verify: scripts/verify_change_scope.py

**Interfaces:**
- Consumes: Tasks 1~3의 Task Scheduler·문서 계약
- Produces: PR 준비 증적과 N100 수동 적용 체크리스트

- [ ] **Step 1: 변경 범위 하네스 실행**

~~~bash
git diff --name-status -z --find-renames origin/main HEAD > /tmp/p0-boot-keepalive-paths.z
python3 scripts/run_change_harness.py --input /tmp/p0-boot-keepalive-paths.z --input-format git-name-status-z --agent-context
~~~

- [ ] **Step 2: 관련 검사 실행 및 하네스 결과 반영**

~~~bash
python3 -m unittest tests.test_windows_bootstrap tests.test_documentation_index tests.test_change_harness tests.test_verify_change_scope -v
python3 scripts/run_change_harness.py --input /tmp/p0-boot-keepalive-paths.z --input-format git-name-status-z --check-result maintenance=success --agent-context
~~~

- [ ] **Step 3: 정적 검사와 독립 검토**

~~~bash
git diff --check origin/main...HEAD
rg -n 'systemctl.*restart.*k3s|kubectl.*secret|persistentvolumeclaim|Caddyfile' scripts/windows-bootstrap.ps1
~~~

검토 기준: KeepAlive가 ONSTART·Password 방식인지, /RP * 외 비밀값 저장이 없는지, InteractiveToken·LogonTrigger가 새 등록 경로에 없는지, 기존 최대 3회 복구 제한과 K3s restart 금지가 유지되는지 확인함.

- [ ] **Step 4: N100 적용 전 체크리스트**

- PR 병합과 사용자 적용 승인을 확인함.
- N100 콘솔에서 InstallTask를 실행하고 운영자가 Windows 계정 암호를 직접 두 번 입력함.
- 로그인하지 않은 상태로 Windows를 재시작함.
- PersonalServer-WSL-KeepAlive가 Running인지, post_boot_check가 passed인지, 외부 health 3회가 모두 HTTP 200인지 확인함.
- 실패 시 재부팅을 반복하지 않고 event log와 Task Scheduler 결과만 수집하여 원인을 분리함.

