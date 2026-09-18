# Telegram Relay 안정화 및 재부팅 이력 보강 설계

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | Telegram Relay 안정화 및 재부팅 이력 보강 설계 |
| 작성일 | 2026-09-18 |
| 기준 자료 | `sre-telegram-relay/app/main.py`, `infra/k8s/sre-telegram/base.yaml`, `scripts/windows-bootstrap.ps1` |
| 목적 | 외부 Telegram 일시 장애가 relay Pod 재시작으로 증폭되는 것을 방지하고, Windows/WSL/K3s 재시작 이후의 복구 이력을 안전하게 남김 |
| 비고 | Secret·PVC·운영 데이터·Caddy·Cloudflare Tunnel ingress는 변경하지 않음 |

## 핵심 요약

- relay의 readiness와 liveness 책임을 분리함. readiness는 Telegram 전달 가능 상태를, liveness는 HTTP 포트 수신 가능 상태를 확인함.
- Windows Supervisor가 시작될 때 비밀값 없는 부팅 식별 시각과 post-boot 복구 결과를 기존 이벤트 로그에 기록함.
- 기존 recovery lock, 구성요소별 최대 3회 복구, inactive K3s만 시작, Portal 단일 rollout restart 제한을 유지함.
- Windows·WSL·K3s 재부팅 자체의 Telegram 알림은 현 운영 정책 범위 밖이므로 추가하지 않음.

## 상세 설계

### 1. Telegram Relay probe 분리

현재 Telegram polling 실패는 relay 상태를 unhealthy로 전환하며 `/healthz`는 HTTP 503을 반환함. readiness와 liveness가 모두 `/healthz`를 사용하면 외부 Telegram 통신 실패가 liveness 실패와 Pod 재시작으로 이어질 수 있음.

| 구분 | 변경 전 | 변경 후 |
|---|---|---|
| readinessProbe | `/healthz` HTTP 확인 | 유지 |
| livenessProbe | `/healthz` HTTP 확인 | `http` 포트 TCP 수신 확인 |
| 이미지·Secret·RBAC | 변경 없음 | 변경 없음 |
| 리소스 requests/limits | 없음 | 실제 OOM·eviction 근거 확보 전 변경하지 않음 |

TCP liveness는 프로세스가 포트를 수신하는지 확인하며, 외부 Telegram API 일시 실패를 프로세스 사망으로 간주하지 않음. readiness가 false인 동안 Alertmanager는 전달 실패를 인지하고 기존 재시도 정책을 따름.

### 2. Windows/WSL/K3s 부팅 이력

`Start-Supervisor`가 초기 복구 점검 전 Windows 부팅 식별 시각을 얻어 기존 `recovery-events.jsonl`에 기록함. 기록은 UTC 시간·구성요소·이벤트·상태·동작만 사용하며, 원문 Event Log·WSL journal·명령 인수·Secret은 저장하지 않음.

| 이벤트 | 기록 시점 | 기록 내용 |
|---|---|---|
| `boot_observed` | Supervisor 시작 직후 | Windows 부팅 시각을 UTC로 정규화한 식별 정보 |
| `post_boot_check` | 기존 초기 복구 점검 | 기존 started/passed/failed 상태 유지 |
| `recovery_dispatch` | 기존 대상 복구 요청·결과 | 기존 구성요소·동작·상태 유지 |

기존 최근 200건 보존 정책을 유지함. Windows·WSL·K3s 재부팅 Telegram 알림은 추가하지 않으며, Cloudflare Tunnel 장애·복구 전환 알림만 기존대로 유지함.

## 변경 범위

| 구분 | 대상 |
|---|---|
| relay 설정 | `infra/k8s/sre-telegram/base.yaml` |
| relay 검증 | `tests/test_k8s_sre_telegram_manifests.py` |
| 자동복구 이력 | `scripts/windows-bootstrap.ps1` |
| 범위·회귀 검증 | `scripts/verify_change_scope.py`, 관련 테스트 |
| 운영 문서 | `docs/codex-work-loop.md`, `docs/public-uptime-monitor.md` |

## 제외 범위

- K3s active 상태에서 전체 restart
- Portal PVC·Secret·운영 데이터·Caddy·Cloudflare Tunnel ingress 변경
- 새 Secret·Telegram token·Chat ID 생성, 저장, 출력
- Windows·WSL·K3s 재부팅 자체의 Telegram 알림 추가
- 실제 OOM 근거 없는 relay resource limit 설정

## 원복 및 검증

| 구분 | 방법 |
|---|---|
| relay 원복 | 기존 HTTP `/healthz` liveness 매니페스트로만 원복함 |
| 자동복구 이력 원복 | 부팅 이력 기록 코드만 직전 검증 버전으로 원복함 |
| 코드 검증 | relay 매니페스트 계약, Windows bootstrap·change scope·관련 문서 테스트 실행 |
| 운영 검증 | relay Ready·restartCount·Kubernetes Event·Prometheus target 상태 확인 |
| 외부 검증 | `https://len.pe.kr/health`를 10초 간격 3회 호출해 HTTP 200 확인 |

## 확인 필요 사항

- relay exit 137의 실제 원인은 OOMKilled·eviction·liveness kill 중 어느 것인지 과거 증적만으로 확정되지 않음. 변경 전 N100의 lastState·Event·이전 로그를 다시 확인함.
- Windows Event Log와 WSL journal 원문의 장기 수집·보관은 보존 기간과 저장 위치가 정해지지 않아 포함하지 않음.
