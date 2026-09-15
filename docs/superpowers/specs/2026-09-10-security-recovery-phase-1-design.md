# N100 보안·복구 1단계 설계

## 문서 정보

| 항목 | 내용 |
|---|---|
| 목적 | 공개 경로의 장애 판정 정확도와 제한형 복구 권한 경계를 강화함 |
| 대상 | N100 자동복구, GitHub 외부 health 감시, HomeOps 제한형 Docker 복구 |
| 제외 | Portal PVC·Secret·운영 데이터·Tunnel ingress·Caddyfile·방화벽 실제 변경 |
| 배포 | 사용자 승인 전에는 배포·push·병합하지 않음 |

## 핵심 요약

1단계는 공개 경로 설정을 변경하지 않고, 실제 Tunnel 연결 단절을 감지하고 Supervisor의 단순 telemetry 실패가 감시 중단으로 이어지지 않게 함. 외부 health 감시는 Telegram 자격증명 상태와 분리함. HomeOps는 관리자 비밀번호를 실행기 비밀값으로 재사용하지 않으며, 별도 실행기 비밀값이 없으면 복구 API를 fail-closed 처리함.

## 설계

### 1. Tunnel 실제 연결 판정

로컬 Caddy→Portal NodePort health가 정상인 경우에만 N100에서 `https://len.pe.kr/health`를 제한 시간 안에 호출함. 로컬 NodePort와 `cloudflared` 서비스·프로세스가 모두 정상인데 공개 health가 실패하면 Tunnel을 비정상으로 분류함. 로컬 NodePort가 비정상이면 Tunnel은 원인으로 단정하지 않고 기존 구성요소별 판정을 유지함.

이 방식은 새 metrics endpoint·자격증명·Tunnel ingress 변경 없이 Cloudflare 경유 실제 사용자 경로를 확인함. N100 인터넷 자체 단절과 Cloudflare 연결 단절은 모두 Tunnel 복구 대상 후보가 될 수 있으므로, 기존 2회 연속 실패·항목별 최대 3회 제한을 그대로 적용함.

### 2. Supervisor fail-open telemetry

Supervisor 초기의 `Update-HostMetrics` 실패는 고정 메시지로 기록하고 120초 대기, 초기 bootstrap, Daemon 기동을 계속함. recovery state 또는 lock을 저장하지 못한 경우에는 중복 실행 방지를 위해 기존 fail-closed 동작을 유지함. 즉, telemetry 기록 실패만 격리하며 복구 횟수 상태 저장 실패는 완화하지 않음.

### 3. 외부 health와 Telegram 분리

GitHub Actions는 Telegram Secret이 없거나 전송이 실패해도 public health 확인과 장애 Issue 생성·종료를 계속 수행함. Telegram 전송 단계에서만 자격증명 누락을 실패로 기록함. Telegram이 전송되지 않은 장애 Issue가 health 복구를 맞으면 “알림 미전송” 증적을 남기고 종료함. 알림 전송 성공 마커가 있는 장애만 Telegram 복구 메시지를 보냄.

### 4. HomeOps 실행기 권한 경계

`HOMEOPS_EXECUTOR_SHARED_SECRET`을 실행기·Portal 간 전용 비밀값으로만 사용하고 `ADMIN_STATUS_PASSWORD` fallback을 제거함. 전용 비밀값이 비어 있으면 Portal과 실행기는 HomeOps 진단·재시작 요청을 거부함. 이 저장소는 비밀값을 생성·출력·저장하지 않으며, 운영자가 기존 승인된 비밀 관리 절차로 사전 설정해야 함.

개별 재시작은 Portal이 승인된 incident의 단회 token을 전달하는 기존 흐름을 유지하되, 실행기는 요청의 action·service allowlist를 계속 검증함. 전체 재시작은 Portal 관리자 인증을 거친 내부 경로만 사용하며, 1단계에서는 Docker socket 자체를 제거하지 않음. Docker socket 대체·권한 프록시는 별도 단계로 분리함.

## 성공 기준

- 로컬 NodePort 정상·공개 health 실패가 Tunnel 비정상으로 판정되는 계약 테스트가 통과함.
- Supervisor telemetry 실패가 Daemon 기동을 막지 않는 계약 테스트가 통과함.
- Telegram Secret 누락 상태에서도 외부 health와 Issue 상태 처리가 계속되는 workflow 계약 테스트가 통과함.
- HomeOps 전용 비밀값 누락 시 관리자 비밀번호 fallback 없이 403으로 차단되는 테스트가 통과함.
- 자동복구 관련 테스트, HomeOps 관련 테스트, 공개 monitor 테스트, 정적 검사 및 외부 health 3회 검증이 통과함.

## 단계 구분

| 단계 | 내용 | 변경 영향 | 승인 게이트 |
|---|---|---|---|
| 1 | 본 설계의 Tunnel 판정·Supervisor·workflow·HomeOps 전용 비밀값 | 제한형 복구와 내부 API | HomeOps 전용 비밀값 사전 설정 확인 필요 |
| 2 | Caddy loopback/방화벽, Cloudflare Access | 공개 경로·접속 방식 | 별도 사용자 승인과 rollback 절차 필요 |
| 3 | 이미지 digest·Actions SHA 고정, 취약점 점검, Supervisor heartbeat | 공급망·운영 자동화 | 별도 설계·CI 검토 필요 |

## 확인 필요 사항

- N100의 `HOMEOPS_EXECUTOR_SHARED_SECRET`이 비어 있지 않은지 값 없이 존재 여부만 확인 필요함.
- 실제 Tunnel active-but-disconnected 훈련은 외부 접속 영향이 있으므로 1단계 코드 검증 뒤 별도 사용자 승인으로 수행 필요함.
- Caddy 80·443의 실제 LAN 바인딩은 N100 SSH 연결이 복구된 뒤 읽기 전용으로 재확인 필요함.
