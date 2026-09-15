# P2 재시작 후 운영 점검 설계

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | P2 재시작 후 운영 점검 설계 |
| 작성일 | 2026-09-14 |
| 기준 자료 | `scripts/windows-bootstrap.ps1`, N100 재시작 진단 결과, 기존 자동복구 정책 |
| 목적 | Windows 재시작 뒤 WSL·K3s·Portal 복귀 상태를 단일 자동복구 경로에서 안전하게 검증함 |
| 비고 | Secret·PVC·운영 데이터·Caddy·Tunnel ingress는 변경하지 않음 |

## 핵심 요약

기존 Windows Supervisor는 시작 뒤 Daemon을 관리하고, Daemon은 3분 주기로 구성요소 상태를 확인함. 본 변경은 Supervisor가 Daemon 시작 전에 1회의 초기 복구 점검을 수행하도록 추가함. 초기 점검은 기존 recovery lock·구성요소별 3회 제한·허용된 복구 동작을 그대로 사용함.

## 설계 범위

### 포함

- Supervisor 초기 대기 후 `Invoke-RecoveryCycle` 1회 실행
- 초기 점검의 시작·완료·실패를 기존 비밀값 없는 `recovery-events.jsonl`에 기록
- Tunnel이 정상인 경우 외부 health를 10초 간격 3회 확인해 초기 점검 완료 기준에 포함
- 로드맵과 운영 문서에 실제 적용 상태·정례 점검 책임을 반영
- 기존 단위 테스트에 초기 점검 순서·health 3회·금지 동작 회귀 조건 추가

### 제외

- 새 Windows 예약 작업 또는 별도 데몬 추가
- K3s가 active인 상태에서 전체 restart
- Portal PVC·Secret·운영 데이터·Caddyfile·Tunnel ingress 수정
- WSL 재시작 자체의 Telegram 알림 추가
- Telegram token·Chat ID 읽기·저장·출력

## 처리 흐름

```text
Windows 시작
  → 기존 Supervisor lock 획득
  → 기존 초기 대기
  → initial_recovery_check 시작 이벤트 기록
  → 기존 Invoke-RecoveryCycle 1회
      → KeepAlive / K3s / Portal / NodePort / Tunnel 상태 확인
      → 필요한 경우에만 기존 허용 복구 동작 수행
      → Tunnel 정상 시 public health 3회 확인
  → 완료 또는 실패 이벤트 기록
  → 기존 Daemon 감독 루프 시작
```

초기 점검은 별도 lock·별도 카운터를 만들지 않음. `Invoke-RecoveryCycle`의 recovery lock과 저장된 구성요소별 failure/attempt counter를 그대로 사용하므로, 동시 실행과 3회 초과 복구를 방지함.

## 오류 처리

| 상황 | 처리 |
|---|---|
| 초기 점검 중 예외 | `postboot_check` 실패 이벤트만 기록하고 Daemon 감독은 계속 시작함 |
| 구성요소가 이미 정상 | 재시작하지 않음 |
| 구성요소별 3회 복구 한도 도달 | 기존 외부 상태 처리 경로로 이관하고 추가 복구하지 않음 |
| public health 3회 중 실패 | Tunnel 상태를 비정상으로 처리하여 기존 Tunnel 전환 알림·복구 정책을 사용함 |
| 상태 파일 저장 실패 | 기존 정책대로 자동 복구 동작을 중단하고 메모리 상태만 유지함 |

## 검증 기준

- `Start-Supervisor`는 초기 대기 뒤 Daemon 실행 전에 초기 recovery cycle을 정확히 1회 호출함
- public health 초기 검증은 성공 3회가 필요하고 호출 사이에 10초 대기함
- active K3s 전체 restart, PVC·Secret·Caddy·Tunnel ingress 변경 명령이 새로 추가되지 않음
- `tests/test_windows_bootstrap.py`, change-scope maintenance 검사, 관련 문서 계약 검사가 통과함
- N100 적용 전후 외부 health를 10초 간격 3회 호출하여 모두 HTTP 200 확인함

## 확인 필요 사항

- Windows 재시작 후 초기 점검 완료를 Telegram으로 별도 알리지 않음. 현재 정책은 Cloudflare Tunnel 장애·복구 전환 알림만 허용함.
- 월간 복구 훈련 실행일·담당자·결과 보관 위치는 운영자가 정해야 함.
