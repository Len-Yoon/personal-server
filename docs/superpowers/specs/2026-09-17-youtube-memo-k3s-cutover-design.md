# YouTube Memo K3s 전환 설계서

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | YouTube Memo K3s 전환 설계서 |
| 작성일 | 2026-09-17 |
| 기준 자료 | `docker-compose.yml`, `docker-compose.n100.yml`, `caddy/Caddyfile`, Book Memo 전환 구현, 현재 운영 상태 |
| 목적 | Docker YouTube Memo를 K3s 단일 writer로 전환하고 공개 경로와 자동 기동 구성을 정합화함 |
| 비고 | 실제 N100 데이터 복사·Docker 중지·Tunnel 변경은 별도 운영 전환 단계에서만 수행함 |

## 2. 핵심 요약

YouTube Memo를 Cloudflare Tunnel → Caddy → K3s Service → Pod → PVC 경로로 전환함. 전환 전까지 Docker만 데이터를 쓰며, 실제 전환 시 Docker 중지·전체 데이터 디렉터리 복사·SQLite 무결성 및 digest 검증·K3s 기동 순서를 강제함. Docker와 K3s가 동시에 데이터를 쓰는 상태는 허용하지 않음.

## 3. 현재 상태와 목표 상태

| 구분 | 현재 | 목표 |
|---|---|---|
| 공개 경로 | Cloudflare Tunnel → `localhost:8002` → Docker | Cloudflare Tunnel → Caddy → K3s Service |
| 실행 주체 | Docker `youtube-memo` | K3s Deployment `youtube-memo` |
| 영속 데이터 | Docker bind mount `data/youtube-memo` | PVC `youtube-memo-data` |
| 쓰기 주체 | Docker 단일 writer | K3s 단일 writer |
| 상태 선언 | `youtube-memo=compose` | `youtube-memo=k3s` |

## 4. 설계

### 4.1 K3s 리소스

- `personal-server` namespace에 `youtube-memo-data` PVC, `youtube-memo` Deployment, ClusterIP Service를 추가함.
- Deployment는 준비 단계에서 replica 0으로 유지함.
- 컨테이너는 Linux AMD64 immutable digest, non-root UID/GID 10001, read-only root filesystem, `/tmp` memory volume, `/health` readiness/liveness probe를 사용함.
- `youtube-memo-runtime` Secret은 존재 여부만 확인하며, 값은 읽거나 저장하지 않음.

### 4.2 데이터 전환

1. Docker가 healthy이고 K3s writer가 없는지 확인함.
2. Docker를 중지하고 중지 상태를 확인함.
3. 데이터 디렉터리 전체를 빈 PVC에 한 번만 복사함. SQLite DB와 인증 rate-limit 상태 파일을 함께 보존함.
4. 원본·PVC 모두 SQLite `quick_check`와 전체 디렉터리 digest를 확인함.
5. 일치할 때만 K3s replica를 1로 확장함.

### 4.3 공개 경로와 자동 기동

- Caddy는 `YOUTUBE_MEMO_UPSTREAM` 환경변수를 사용하며, K3s 상태에서는 Deployment·Service·Endpoint·PVC 확인 후 ClusterIP:8002만 설정함.
- Caddy의 Docker YouTube Memo `depends_on`을 제거함.
- 일반 배포, 안전 배포, health 검증, HomeOps 관리 목록은 runtime state에서 `youtube-memo=k3s`를 읽어 Docker YouTube Memo를 기동하거나 loopback health를 요구하지 않도록 처리함.
- runtime state는 K3s readiness가 확인된 직후에만 root 소유 상태 파일로 갱신함.

## 5. 전환 순서

| 순서 | 단계 | Docker writer | K3s writer | 공개 요청 | 성공 기준 |
|---:|---|---|---|---|---|
| 1 | 코드·이미지·Secret·manifest 준비 | 실행 | 없음 | Docker | 정적·준비 검증 통과 |
| 2 | K3s 내부 준비 | 실행 | 없음 | Docker | PVC Bound, replica 0 |
| 3 | 유지보수 전환 | 중지 | 없음 | 일시 중단 가능 | 원본 데이터 무결성 확인 |
| 4 | 데이터 복사·K3s 시작 | 중지 | 시작 | 일시 중단 | digest 일치, Pod Ready |
| 5 | runtime state·Caddy 전환 | 중지 | 실행 | K3s | Caddy에서 K3s health 성공 |
| 6 | Tunnel 전환·외부 검증 | 중지 | 실행 | K3s | 외부 `/health` 3회 HTTP 200 |

## 6. 롤백 경계

K3s에서 새 쓰기가 발생하기 전이고 Docker 원본과 PVC 데이터 digest가 동일할 때만 Docker로 복귀할 수 있음. K3s에서 새 메모가 생성된 뒤에는 Docker를 단순 재기동하지 않으며, 데이터 복구 계획을 별도로 수립해야 함.

## 7. 제외 범위

- Book Memo와 Portal의 실행 경로·PVC·Secret·운영 데이터는 변경하지 않음.
- 새 외부 레지스트리, 새 Secret 값, 새 자격 증명은 생성하지 않음.
- 실제 Cloudflare Tunnel ingress 변경, Docker 중지, PVC 데이터 복사는 준비 검증·독립 검토·사용자 전환 승인 뒤에만 수행함.

## 8. 검증 기준

| 구분 | 검증 기준 |
|---|---|
| 코드 | 기존 YouTube Memo 단위 테스트 및 새 K3s 전환 계약 테스트 통과 |
| 이미지 | Linux AMD64 OCI archive digest와 N100 import 결과 일치 |
| K3s | Deployment Ready, Service Endpoint, PVC Bound, `/health` 성공 |
| 데이터 | SQLite `quick_check` 성공 및 전체 데이터 digest 일치 |
| 외부 | `https://memo.len.pe.kr/health` 10초 간격 3회 HTTP 200 |
| 단일 writer | K3s 상태에서 Docker YouTube Memo가 실행 중이 아님 |

## 9. 확인 필요 사항

| 항목 | 내용 |
|---|---|
| 운영 Secret | `youtube-memo-runtime`의 존재 및 기존 Docker 환경과 의미상 동등한지 확인 필요 |
| N100 저장소 | PVC 용량과 non-root 쓰기 권한 확인 필요 |
| 실제 전환 창 | Docker 중지와 Tunnel 변경을 수행할 사용자 승인 시점 확인 필요 |

## 10. 후속 조치

1. 매니페스트·준비·cutover·Caddy/runtime-state 계약을 테스트 우선으로 구현함.
2. 관련 정적 검사와 독립 검토를 수행함.
3. 저장소 반영 승인 후 N100 이미지 반입·사전 검증을 수행함.
4. 실제 데이터 전환과 외부 경로 변경은 별도 승인 후 실행함.
