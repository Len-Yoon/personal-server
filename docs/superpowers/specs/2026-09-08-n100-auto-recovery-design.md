# N100 자동 복구 감시 설계

## 1. 목적

Windows 기반 N100에서 WSL, K3s Portal, Cloudflare Tunnel 중 하나가 비정상이어도 자동으로 상태를 확인하고 필요한 구성요소만 복구함. 정상 서비스의 재생성이나 Portal 데이터 경로 변경은 수행하지 않음.

## 2. 범위

| 구분 | 포함 | 제외 |
|---|---|---|
| 감시 주기 | 3분마다 내부 상태 확인 | 외부 상태 점검 주기 변경 |
| 복구 조건 | 같은 상태가 2회 연속 실패 | 단일 일시 실패에 대한 즉시 재시작 |
| 대상 | WSL 유지 작업, K3s, Portal NodePort, Cloudflare Tunnel | Portal 애플리케이션, PVC, Compose Portal writer, Caddy 설정 |
| 알림 | 기존 GitHub Actions 외부 상태 점검 및 Telegram 알림 유지 | 새 Telegram 토큰·Secret 생성 또는 저장 |

## 3. 동작 설계

### 3.1 감시 흐름

`personal-server-autostart`의 기존 Daemon이 3분마다 다음 순서로 확인함.

1. WSL 배포판 실행 가능 여부 확인함.
2. K3s 서비스 active 여부와 Kubernetes API 응답을 확인함.
3. `personal-server` 네임스페이스의 `portal-web` Deployment Available 상태를 확인함.
4. 로컬 NodePort `127.0.0.1:30080/health` 응답을 확인함.
5. Cloudflare Tunnel 프로세스 존재 여부를 확인함.

모든 항목이 정상일 때 실패 횟수를 초기화함. 항목별 실패 상태는 로컬 상태 파일에만 기록하며 비밀값은 기록하지 않음.

### 3.2 복구 조건과 순서

동일 항목이 2회 연속 실패한 경우에만 아래의 최소 복구를 수행함.

| 실패 항목 | 복구 동작 | 검증 | 제한 |
|---|---|---|---|
| WSL 또는 KeepAlive | 기존 KeepAlive 작업을 시작함 | WSL 명령 실행 가능 여부 | 한 감시 주기당 1회 |
| K3s inactive 또는 API 실패 | K3s 서비스만 시작함 | API 응답 및 Portal Available 대기 | 1회 후 다음 주기에 재평가 |
| Portal 미가용 | Kubernetes의 기본 Pod 복구를 먼저 대기함 | Deployment Available 및 NodePort health | 연속 실패 시에만 Deployment restart 1회 |
| NodePort 미응답, Portal 정상 | Caddy만 기존 bootstrap 경로로 보장함 | NodePort health | 1회 후 다음 주기에 재평가 |
| Tunnel 미실행 | 기존 `Start-CloudflareTunnel`만 호출함 | 프로세스 존재 여부 | 한 감시 주기당 1회 |

복구 동작은 하나의 실행 잠금을 사용함. 이전 복구가 끝나기 전 다음 감시 실행은 복구를 추가로 시작하지 않음.

### 3.3 중단과 알림

같은 항목의 자동 복구가 3회 연속 실패하면 추가 재시작을 중단하고 실패 상태를 유지함. 외부 공개 상태의 장애·복구 알림은 기존 GitHub Actions와 Telegram 경로가 담당함. 따라서 새 토큰, Secret 또는 외부 알림 전송 기능을 추가하지 않음.

## 4. 안전 경계

- Portal PVC, Kubernetes Secret, 데이터, Caddyfile, Tunnel ingress 설정은 수정하지 않음.
- Portal Compose writer는 시작하거나 재생성하지 않음.
- K3s가 active인 상태에서는 K3s 전체 재시작을 수행하지 않음.
- Windows 자체 종료, 하드웨어 장애, 인터넷 단절은 자동 복구 보장 범위 밖이며, 감시 결과와 외부 알림으로만 확인 가능함.

## 5. 검증 기준

1. 단위 테스트에서 정상 상태가 복구 명령을 실행하지 않음을 확인함.
2. 연속 실패 횟수, 잠금, 복구 횟수 제한을 검증함.
3. PowerShell 구문 검사와 관련 기존 bootstrap 테스트를 통과함.
4. N100에서는 실제 서비스 중단 없이 정상 감시 결과와 상태 기록을 확인함.
5. 독립 운영 검토에서 복구 순서, 중복 실행 방지, 금지 영역 침범 여부를 확인함.
