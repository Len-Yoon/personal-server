# 제한형 무인 자동복구 SRE 설계

## 1. 목적

Windows N100과 WSL2에서 운영하는 개인 서버가 Tunnel·WSL 유지·K3s·Portal의 제한된 장애를 자동 감지하고, 승인된 범위 안에서 복구한 뒤 GitHub Actions가 외부 공개 상태를 확인하여 Telegram으로 장애·복구 전환을 알리도록 구성함.

목표는 모든 장애를 무조건 재시작하는 것이 아니라, 데이터와 공개 경로 설정을 보존하면서 반복 가능한 장애만 제한적으로 자동복구하는 것임.

## 2. 범위와 제외 범위

| 구분 | 대상 | 처리 기준 |
|---|---|---|
| 자동 감시 | Windows WSL 유지 작업, WSL 사용자 Tunnel 서비스, K3s, Portal 가용성, Caddy 경유 NodePort | 3분 주기 로컬 감시 |
| 자동 복구 | 중지된 WSL 유지 작업, 중지된 K3s, 가용하지 않은 Portal, 중지·비정상 Tunnel 서비스 | 2회 연속 실패 뒤 항목별 최대 3회 |
| 외부 검증 | `https://len.pe.kr/health` | GitHub Actions가 약 5분 주기로 확인 |
| 알림 | 외부 장애·복구 Telegram 알림과 GitHub Issue 증적 | GitHub Actions만 전송 |
| 제외 | Portal PVC·Secret·운영 데이터·Caddyfile·Tunnel ingress·Compose Portal writer | 자동 수정 및 자동 재시작 금지 |
| 제외 | 정전, 공유기·회선 장애, Cloudflare·GitHub·Telegram 자체 장애 | 별도 장비·외부 사업자 상태가 필요하므로 자동복구 보장 대상 아님 |

N100에는 Telegram Bot token이나 GitHub 토큰을 저장하지 않음. Telegram 메시지는 GitHub Actions의 Repository secret만 사용함.

## 3. 목표 구조

```text
Windows Scheduled Task (무기한 실행, 실패 시 제한 재시작)
  → N100 로컬 상태 확인
  → 제한형 복구 실행
  → WSL Tunnel · K3s · Portal
  → Cloudflare Tunnel · Caddy · Portal 공개 경로
  → GitHub Actions 외부 health 확인
  → Telegram 장애·복구 전환 알림과 Issue 증적
```

N100은 복구 요청과 로컬 상태 확인만 담당함. 외부 공개 정상 여부의 최종 판정과 Telegram 알림은 GitHub Actions가 담당함.

## 4. 복구 계약

### 4.1 공통 계약

1. 항목별 상태는 `healthy`, `unhealthy`, `deferred`로 구분함.
2. 동일 항목이 2회 연속 `unhealthy`일 때만 복구를 시작함.
3. 항목별 복구 시도는 최대 3회로 제한함.
4. 복구 상태 저장에 실패하면 추가 자동 조작을 중단함.
5. 정상 상태가 확인되면 해당 항목의 실패·시도 횟수를 초기화함.
6. 복구 명령의 성공은 서비스가 실행되었다는 뜻일 뿐 외부 공개 정상 전환을 뜻하지 않음.

### 4.2 Tunnel 계약

| 단계 | 현재 한계 | 목표 동작 |
|---|---|---|
| 감지 | `cloudflared` 프로세스 존재만 확인 | WSL 사용자 `cloudflared-personal-server.service` 활성 상태와 프로세스를 함께 확인 |
| 복구 | 별도 `cloudflared tunnel run` 프로세스 실행 | 등록된 사용자 서비스를 시작 또는 재시작해 단일 소유자를 유지 |
| 로컬 검증 | 실행 요청 결과만 확인 | 서비스 active 및 연결 프로세스 재확인 |
| 외부 검증 | N100 복구기에서 확인하지 않음 | GitHub Actions가 공개 `/health` 정상 전환을 최종 판정 |
| 알림 | N100은 전송하지 않음 | GitHub Actions가 장애·복구 전환마다 1회 전송 |

Tunnel의 수동 중지는 복구 훈련에만 사용함. 자동복구가 동작하면 같은 기준으로 서비스를 다시 시작해야 함.

### 4.3 K3s와 Portal 계약

- K3s는 `inactive`일 때만 시작하며, 실행 중인 K3s를 재시작하지 않음.
- Portal은 K3s API가 정상이고 Deployment가 가용하지 않을 때만 rollout restart를 시도함.
- NodePort 단독 이상은 Portal 재시작으로 우회하지 않고 다음 주기에 재판정함.
- Portal PVC·Secret·운영 데이터·Compose Portal writer는 복구 동작에서 제외함.

## 5. 상태 전이와 알림

| 외부 상태 | GitHub Actions 동작 | Telegram | Issue |
|---|---|---|---|
| 정상 | 상태 변경 없음 | 발송 안 함 | 없음 |
| 정상 → 장애 | 장애 이슈 생성 후 Telegram API 수락 확인 | 장애 1회 | 열림 |
| 장애 유지 | 중복 알림 억제 | 발송 안 함 | 열림 유지 |
| 장애 → 정상 | Telegram JSON 성공 응답 검증 후 복구 증적 기록 | 복구 1회 | 닫힘 |

Telegram API 응답의 `ok=true`와 메시지 ID 형식이 검증된 뒤에만 전환을 확정함. token, chat ID, 메시지 ID, 응답 본문은 로그·Issue·Git에 기록하지 않음.

## 6. 구현 단계

### 단계 1: 실제 Tunnel 서비스 기준으로 복구 경로 통일

- Windows 감시 작업에서 WSL 사용자 서비스의 상태·시작 가능 여부를 확인하는 전용 경로를 추가함.
- 임시 `cloudflared` 프로세스 직접 실행을 제거하고, 단일 systemd 사용자 서비스만 제어함.
- Windows Scheduled Task의 실행 사용자와 WSL `window` 사용자의 systemd user bus 접근 조건을 실제 환경에서 검증함.

성공 기준은 Tunnel 중지 뒤 감시 작업이 정해진 실패 횟수 이후 같은 사용자 서비스를 시작하고, 중복 `cloudflared` 프로세스를 만들지 않는 것임.

### 단계 2: 복구 후 로컬 재검증과 실패 기록 강화

- 각 복구 시도 뒤 서비스 활성 여부를 재확인함.
- 실패 사유는 비밀값 없이 상태 파일과 Windows 작업 로그에 남김.
- 최대 시도 도달, 상태 저장 실패, 복구 명령 실패를 구분함.

성공 기준은 실패 시도 횟수가 영속되고, 네 번째 자동 시도가 발생하지 않는 것임.

### 단계 3: 외부 상태·알림 연계 검증

- GitHub Actions 공개 상태 monitor의 기존 장애·복구 전환 계약을 유지함.
- 복구 Issue는 Telegram API 수락 검증 뒤에만 닫힘.
- GitHub 예약 지연은 허용하되, 복구 훈련에서는 수동 실행으로 즉시 외부 전환을 확인함.

성공 기준은 외부 장애와 정상 전환에서 Telegram 메시지가 각각 한 번 수신되고, 복구 증적이 Issue에 남는 것임.

### 단계 4: 재부팅·장애 훈련과 롤백 검증

- Tunnel 중지, WSL 유지 작업 중지, 격리된 Portal 가용성 실패를 각각 별도 훈련함.
- 각 훈련은 한 항목만 변화시키고, 정상 상태·외부 health·Telegram 결과를 기록함.
- 실패 시 자동복구 작업을 중지하고 기존 `cloudflared-personal-server.service`를 수동 시작하는 롤백 절차를 사용함.

성공 기준은 실서비스 데이터·Secret·PVC·Caddy·Tunnel ingress 변경 없이 각 훈련을 복구하는 것임.

## 7. 테스트와 검증 기준

| 구분 | 검증 |
|---|---|
| 단위·계약 테스트 | Windows 복구 스크립트의 사용자 서비스 제어, 실패 임계값, 최대 시도, 중복 실행 방지, 금지 경로 보존 |
| 정적 검사 | PowerShell 구문, workflow YAML·JavaScript 계약, `git diff --check` |
| 독립 운영 검토 | Windows Scheduled Task, WSL user systemd, 외부 monitor, 시크릿 경계, rollback 검토 |
| 실제 훈련 | Tunnel만 중지한 뒤 자동복구, 외부 health, 장애·복구 Telegram 전환 확인 |
| 재부팅 검증 | Windows 시작 뒤 WSL 유지·Tunnel·감시 작업·공개 health 확인 |

실제 훈련은 외부 공개 장애를 일시적으로 유발하므로 사용자 승인 아래 수행함. 훈련 중 Tunnel이 예상 시간 안에 복구되지 않으면 즉시 수동으로 사용자 서비스를 시작하고, 추가 자동 변경 없이 원인을 분석함.

## 8. 완료 기준과 잔여 한계

다음 조건을 모두 충족할 때 제한형 무인 자동복구 SRE로 완료 처리함.

1. Windows 작업이 재부팅 뒤 무기한 실행되고 비정상 종료 시 제한 재시작됨.
2. Tunnel 중지 훈련에서 N100이 수동 개입 없이 서비스를 복구함.
3. 외부 `/health`가 정상 전환되고 GitHub Actions가 복구 Telegram을 1회 전송함.
4. 반복 실패에서 자동 조작이 최대 시도 뒤 중단됨.
5. Portal 데이터, Secret, PVC, Caddy, Tunnel ingress가 변경되지 않음.

정전·네트워크 회선 장애·Cloudflare·GitHub·Telegram 사업자 장애와 Telegram 기기 푸시 설정은 본 설계의 자동복구 보장 범위 밖임.
