# Crawler Worker K3s 전환 설계서

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | Crawler Worker K3s 전환 설계서 |
| 작성일 | 2026-09-17 |
| 기준 자료 | `docker-compose.yml`, `docker-compose.n100.yml`, `scripts/runtime-service-state.sh`, Book·YouTube Memo 전환 구현, crawler 관측성 manifest |
| 목적 | Docker Compose `crawler-worker`를 K3s 단일 writer로 전환하면서 뉴스 수집 상태·알림 중복 방지 상태, metrics, `NewsCollectionStale`, Telegram 경계를 보존함 |
| 비고 | 실제 Docker 중지·PVC 복사·Tunnel/Caddy 전환은 테스트·독립 검토·별도 운영 승인 후에만 수행함 |

## 2. 핵심 요약

뉴스 수집기를 Cloudflare Tunnel → Caddy → K3s Service → Pod → PVC 경로로 전환함. 실제 전환 전까지 Docker만 수집 상태를 쓰며, 전환 중 scheduler를 포함한 Docker writer를 중지한 뒤 전체 데이터 디렉터리를 빈 PVC에 한 번만 복사함. K3s Pod가 수집을 시작한 뒤 Docker를 동시에 기동하지 않음.

현재 `crawler-worker=k3s` runtime state 형식과 K3s 전환 runner 정책은 이미 존재함. 다만 현재 K3s 앱 manifest·준비/전환 도구·native ServiceMonitor 경로는 없으며, Caddy는 `news.len.pe.kr`을 Docker hostname으로 고정함. 이 세 경계를 함께 정합화해야 단일 writer와 경고 연속성이 보장됨.

## 3. 현재 상태와 목표 상태

| 구분 | 현재 | 목표 |
|---|---|---|
| 공개 경로 | Cloudflare Tunnel → Docker Compose crawler endpoint | Cloudflare Tunnel → Caddy → K3s `crawler-worker` Service |
| 실행 주체 | Docker `crawler-worker` | K3s Deployment `crawler-worker` |
| 영속 데이터 | `data/crawler-worker` bind mount | `crawler-worker-data` PVC |
| 수집 writer | Docker scheduler 단일 writer | K3s scheduler 단일 writer |
| metrics 수집 | Compose bridge Service `compose-crawler` | K3s `crawler-worker` Service |
| 상태 선언 | `crawler-worker=compose` | `crawler-worker=k3s` |

## 4. 설계

### 4.1 K3s 리소스와 런타임

- `personal-server` namespace에 `crawler-worker-data` PVC, `crawler-worker` Deployment, ClusterIP Service를 추가함.
- 준비 단계 Deployment는 replica 0으로 유지함. 실제 전환 뒤에만 replica 1로 확장함.
- 컨테이너는 Linux AMD64 immutable digest, UID/GID 10001, read-only root filesystem, `/tmp` memory volume, `/health` readiness/liveness probe를 사용함.
- `crawler-worker-runtime` Secret은 존재 여부만 확인함. 값은 읽거나 Git·로그·문서에 기록하지 않음.
- 승인된 유지보수 구간에서 새 HomeOps의 runtime marker 읽기 전용 mount를 적용한 뒤, Docker 중지 전에 `crawler-worker=k3s`로 갱신해 자동복구 재시작을 차단함. 이 marker 선갱신은 실제 K3s 준비 완료 선언이 아니며, K3s Endpoint 준비 전 일반 배포·Caddy 재생성을 수행하지 않음.

### 4.2 데이터와 scheduler 전환

1. Docker health, bind mount, 현재 writer가 사용하는 `news_archive.json`·`news_collection_status.json` layout, K3s writer 부재를 확인함.
2. 자동 배포·복구·수동 재시작을 배제하고 진행 중 작업 종료를 확인함. 새 HomeOps를 별도로 적용하고 marker를 `crawler-worker=k3s`로 변경해 실행 중 executor에서 crawler 제외를 확인한 뒤 Docker crawler를 중지함.
3. 빈 PVC에 `data/crawler-worker` 전체를 복사함. 수집 상태 파일과 알림 중복 방지 상태를 함께 보존함.
4. 원본·PVC의 전체 디렉터리 SHA-256 digest를 비교함. SQLite 파일이 실제 존재하면 `quick_check`를 추가로 수행함.
5. 일치할 때만 K3s replica를 1로 확장함. rollout 직후 Docker 중지·HomeOps 제외를 재확인함. 실패 시 양쪽 writer를 멈추고 데이터가 같은 경우에만 Docker 복구를 허용하며, 데이터가 달라졌거나 검증 불가이면 양쪽을 중지하고 원본·PVC를 보존함.

### 4.3 공개 경로·관측성

- Caddy는 `CRAWLER_WORKER_UPSTREAM` 환경변수를 사용함. K3s 상태에서는 검증된 Service ClusterIP:8001만 전달함.
- Cloudflare Tunnel의 `news.len.pe.kr`은 Docker loopback 직접 ingress에서 WSL loopback Caddy로 전환함. Tunnel과 Caddy가 Docker/K3s 양쪽으로 요청을 분산하는 구성은 허용하지 않음.
- `crawler-news-observability` ServiceMonitor는 Compose bridge label 대신 K3s crawler Service label을 선택함. metrics 경로·인증 Secret 이름·`NewsCollectionStale` 경고 의미는 유지함.
- Caddy, Tunnel, ServiceMonitor의 공개/관측 전환은 K3s Deployment·Endpoint·인증 metrics가 확인된 뒤에만 수행함.

## 5. 전환 순서

| 순서 | 단계 | Docker writer | K3s writer | 공개 요청 | 성공 기준 |
|---:|---|---|---|---|---|
| 1 | 코드·이미지·Secret·manifest 준비 | 실행 | 없음 | Docker | 정적 검증·준비 검증 통과 |
| 2 | K3s 내부 준비 | 실행 | 없음 | Docker | PVC Bound, replica 0 |
| 3 | 유지보수·HomeOps 반영·marker 선갱신 후 Docker 중지 | 중지 | 없음 | 일시 중단 가능 | executor crawler 제외, 원본 상태 파일 검증 |
| 4 | 데이터 복사·K3s 시작 | 중지 | 실행 | 일시 중단 | digest 일치, Pod Ready |
| 5 | Docker 중지 재확인·Caddy·metrics 전환 | 중지 | 실행 | K3s | HomeOps 제외 유지, K3s Service metrics·Caddy 내부 health 성공 |
| 6 | Tunnel 전환·외부 검증 | 중지 | 실행 | K3s | `news.len.pe.kr/health` 3회 HTTP 200, Telegram 경고 규칙 inactive |

## 6. 롤백 경계

K3s에서 새 수집 또는 상태 파일 갱신이 발생하기 전이고 Docker 원본과 PVC digest가 동일할 때만 Docker writer로 복귀할 수 있음. K3s가 새 뉴스·알림 상태를 기록한 뒤에는 Docker를 단순 재기동하지 않음. 이 경우 더 최신인 PVC를 보존하고 역방향 데이터 복구는 별도 승인·계획으로 처리함.

## 7. 제외 범위와 권한 경계

- Portal, Book Memo, YouTube Memo, 차량관리, 기존 PVC·Secret 값·Telegram 자격 증명은 변경하지 않음.
- 새 외부 레지스트리, 새 자격 증명, Secret 값은 생성·출력·저장하지 않음.
- 현재 저장소 `AGENTS.md`는 Caddy·Tunnel·운영 데이터 변경을 일반적으로 금지함. 따라서 구현 착수 전 crawler-worker 전환에 필요한 최소 파일과 실제 N100 전환 권한을 명시적으로 확장해야 함.
- 실제 N100 Docker 중지, PVC 복사, Caddy/Tunnel 변경은 이 설계와 구현 검증이 끝난 뒤 별도 사용자 승인으로만 수행함.

## 8. 검증 기준

| 구분 | 검증 기준 |
|---|---|
| 코드 | crawler 단위 테스트와 신규 K3s 전환 계약 테스트 통과 |
| 이미지 | Linux AMD64 OCI archive digest와 N100 import 결과 일치 |
| K3s | Deployment Ready, Service Endpoint, PVC Bound, `/health` 성공 |
| 데이터 | 전체 데이터 digest 일치, 존재하는 SQLite 파일의 `quick_check` 성공 |
| 관측성 | 인증 metrics, K3s ServiceMonitor target, `NewsCollectionStale` 규칙 확인 |
| 외부 | `https://news.len.pe.kr/health` 10초 간격 3회 HTTP 200 |
| 단일 writer | K3s 상태에서 Docker crawler가 실행 중이 아님 |

## 9. 확인 필요 사항

| 항목 | 확인 내용 | 영향 |
|---|---|---|
| 운영 Secret | `crawler-worker-runtime` 존재 및 기존 Docker 환경과 의미상 동등성 | Pod 기동 전 확인 필요 |
| 데이터 경로 | 실제 bind mount·상태 파일·데이터 크기 | 복사 범위와 digest 기준 확정 필요 |
| scheduler | Docker 중지 뒤 K3s Pod 기동 시 즉시 수집/알림 여부 | 전환 시 중복 알림 방지 필요 |
| ServiceMonitor | 현재 Prometheus target·인증 Secret과 K3s label 계약 | 경고 공백 방지 필요 |
| 운영 권한 | Caddy·Tunnel·runtime state·PVC 변경 허용 범위 | 구현·실제 전환의 선행 조건 |

## 10. 후속 조치

1. 저장소 운영 규칙에 crawler-worker 전환의 최소 수정 범위와 실제 N100 승인 경계를 반영함.
2. manifest·준비·cutover·Caddy/runtime-state·ServiceMonitor 계약을 테스트 우선으로 구현함.
3. 저장소 검증과 독립 검토 후 이미지 반입·K3s 준비를 수행함.
4. Docker 중지·데이터 복사·공개 경로 전환은 별도 승인 후 수행함.
