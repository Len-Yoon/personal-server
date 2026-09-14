# P0 무로그인 KeepAlive 부팅 복구 설계

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | P0 무로그인 KeepAlive 부팅 복구 설계 |
| 작성일 | 2026-09-14 |
| 기준 자료 | N100 재부팅 검증 이벤트, `scripts/windows-bootstrap.ps1`, Windows 예약 작업 XML |
| 목적 | Windows 로그인 전에도 WSL KeepAlive와 재시작 뒤 초기 점검이 동작하도록 예약 작업 등록 경로를 보완함 |
| 범위 | KeepAlive 예약 작업 등록, 계약 테스트, 운영 문서 |

## 2. 문제 정의

`personal-server-autostart`는 `BootTrigger`와 Password 방식으로 시작되지만, `PersonalServer-WSL-KeepAlive`는 `LogonTrigger`와 `InteractiveToken` 방식임. 따라서 Windows 로그인 전에는 KeepAlive가 실행되지 않고, Supervisor의 `post_boot_check`가 KeepAlive 비정상으로 실패할 수 있음.

## 3. 목표와 성공 기준

### 3.1 목표

`-InstallTask` 실행 시 KeepAlive 작업을 Supervisor와 같은 부팅 실행 조건으로 등록하여 로그인 없는 Windows 부팅에서도 WSL 유지 경로를 시작함.

### 3.2 성공 기준

- KeepAlive 작업은 `BootTrigger`와 Password LogonType을 사용함.
- KeepAlive 작업은 기존 WSL 명령(`Ubuntu-24.04`, root, 무기한 sleep)을 유지함.
- KeepAlive 작업은 단일 인스턴스·무기한 실행·실패 시 제한된 재시작 설정을 유지함.
- 저장소·로그·명령 출력에 Windows 계정 비밀번호, Telegram 값 또는 다른 비밀값을 저장·출력하지 않음.
- 적용 뒤 로그인 없는 Windows 재시작에서 KeepAlive가 Running이고 `post_boot_check`가 passed이며, 외부 health를 10초 간격으로 3회 호출해 모두 HTTP 200임.

## 4. 설계

### 4.1 예약 작업 등록

`scripts/windows-bootstrap.ps1`에 `$KeepAliveTaskName`과 `Install-KeepAliveTask`를 추가함. `Install-ScheduledTask`는 기존 Supervisor 등록 전에 KeepAlive 등록을 호출함.

KeepAlive 등록은 `schtasks.exe /Create`를 사용하며 다음 계약을 가짐.

| 항목 | 값 |
|---|---|
| 작업명 | `PersonalServer-WSL-KeepAlive` |
| Trigger | `/SC ONSTART` |
| 실행 계정 | 현재 Windows 사용자 (`DOMAIN\\user`) |
| 로그인 방식 | `/RP *`를 통한 Task Scheduler Password 방식 |
| 실행 명령 | `wsl.exe -d Ubuntu-24.04 -u root --exec /bin/bash -lc "while true; do sleep 3600; done"` |
| 실행 시간 제한 | `PT0S` |
| 중복 실행 | IgnoreNew |

`/RP *`는 Windows가 운영자에게 직접 암호를 입력받도록 하며, 스크립트는 비밀번호를 변수·파일·로그에 보관하지 않음. 설치 시에는 Supervisor와 KeepAlive 각각에 대해 Windows 암호 입력이 요청될 수 있음.

### 4.2 장애 처리와 안전 경계

- KeepAlive 등록 실패 시 `-InstallTask`를 실패 처리하여 로그인 의존 상태를 정상 구성으로 오인하지 않음.
- 기존 `Start-ScheduledTask -TaskName "PersonalServer-WSL-KeepAlive"` 복구 동작, 최대 3회 시도 제한, K3s active 상태 재시작 금지는 변경하지 않음.
- Portal PVC·Secret·운영 데이터·Caddyfile·Cloudflare Tunnel ingress와 Telegram 자격증명 처리 경로는 변경하지 않음.
- `SYSTEM` 계정은 사용자 WSL 배포판과 `window` 사용자 Tunnel 서비스의 소유 경계가 달라 사용하지 않음.

### 4.3 검증

정적 계약 테스트는 KeepAlive 등록에 ONSTART, `/RP *`, 기존 WSL 명령, Password 방식 및 금지된 InteractiveToken·LogonTrigger 부재를 확인함. 운영 적용 전후에는 PowerShell 구문 검사와 관련 Python 테스트를 실행함.

실제 N100 적용은 병합 후 사용자 승인 뒤에만 진행함. Windows에서 `-InstallTask`를 실행할 때 운영자가 암호를 직접 입력하고, 로그인하지 않은 상태로 재부팅하여 KeepAlive·`post_boot_check`·외부 health 3회를 확인함.

## 5. 제외 범위

- Windows 계정·암호·sudo 권한·Telegram 자격증명의 생성, 출력, Git 저장
- Portal PVC, Kubernetes Secret, 운영 데이터, Caddyfile, Cloudflare Tunnel ingress 수정
- K3s active 상태 재시작, Portal 이외 Workload 재시작, 무제한 재시작 로직
- N100 자동 적용 및 무승인 재부팅

## 6. 확인 필요 사항

- Windows Task Scheduler는 Password 방식 작업 생성 시 암호 입력을 요구함. N100 운영자가 콘솔에서 직접 입력 가능한 상태인지 적용 전에 확인 필요함.
- 실제 N100 검증은 로그인하지 않은 부팅 상태에서 수행하므로, 원격 접속 가능 경로를 유지한 승인된 점검 창에서만 수행 필요함.
