# Task 1 구현 보고서

## 담당 범위

- `scripts/windows-bootstrap.ps1`
- `tests/test_windows_bootstrap.py`
- 초기 Supervisor 점검 함수 및 외부 Portal health 3회 검증 구현

## 구현 내용

- `Test-PublicPortalHealthThreeTimes` 추가
  - `Test-PublicPortalHealth` 성공을 최대 3회 확인함
  - 성공 호출 사이에 10초 대기함
- `Invoke-PostBootRecoveryCheck` 추가
  - 기존 `Write-RecoveryEvent`로 `post_boot_check` 시작·성공·실패를 기록함
  - 기존 `Invoke-RecoveryCycle`을 재사용하여 recovery lock·카운터·허용 복구 동작을 공유함
  - 점검 실패를 내부에서 처리하여 Daemon 감독 루프가 계속 시작되도록 함
- `Start-Supervisor`에서 기존 120초 부팅 대기 및 stack bootstrap 뒤, Daemon `Start-Process` 전에 초기 점검을 1회 호출함

## TDD 증적

### RED

신규 계약 테스트 작성 후 다음 명령을 실행함.

```text
python3 -m unittest tests.test_windows_bootstrap.WindowsBootstrapTests.test_supervisor_runs_one_post_boot_check_before_starting_daemon tests.test_windows_bootstrap.WindowsBootstrapTests.test_post_boot_public_health_requires_three_checks -v
```

결과: 신규 테스트 2건이 함수·호출 부재로 실패함.

### GREEN

최소 구현 후 동일 명령 결과: 신규 테스트 2건 모두 통과함.

## 검증 결과

- `python3 -m unittest tests.test_windows_bootstrap -v`: 71건 통과
- `git diff --check`: 통과
- 금지 동작 검색(`restart.*k3s`, `delete.*persistentvolumeclaim`, `kubectl.*secret`, `Caddyfile`): 신규 일치 항목 없음

## 커밋

- `979bccc feat: 재시작 후 초기 복구 점검 추가`

## 우려 사항

- `Invoke-RecoveryCycle` 자체의 Tunnel health probe 1회 이후 초기 점검에서 외부 health를 추가 3회 확인하므로, 초기 Supervisor 점검 중 외부 health 호출은 총 4회가 될 수 있음. 이는 기존 recovery cycle 재사용과 별도 3회 외부 검증 요구를 함께 만족하기 위한 구조이며 운영 문서 확인 필요함.
- 실제 Windows/WSL/N100 환경 실행 및 외부 `https://len.pe.kr/health` HTTP 200 검증은 이 작업공간에서 수행하지 않음.
