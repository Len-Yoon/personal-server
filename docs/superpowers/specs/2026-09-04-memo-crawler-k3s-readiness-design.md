# Crawler·Memo K3s 이행 준비 설계

## 목적과 증거 범위

N100 원격 개발 환경 준비 후 `crawler-worker`, `youtube-memo`, `book-memo`의 Compose→K3s 이행 준비 상태를 **저장소 증거만으로** 판정하고, 다음 코드 전용 작업을 정한다. 기준 revision은 `b3312db`이며 조사 대상은 Compose 정의, 서비스 소스·Dockerfile·테스트, `infra/k8s/` 초안, 전환 실행기 artifact와 기존 전환 문서다.

클러스터 API, N100 파일시스템, 실제 PVC/Pod/Secret/이미지, Compose 실행 상태, backup artifact, Caddy와 공개 라우팅은 조사하지 않았다. 따라서 “미확인”은 운영 실패가 아니라 이 저장소만으로 확인할 수 없다는 뜻이다.

## 현재 상태 평가

| 영역 | 저장소에서 확인한 사실 | 결론 |
| --- | --- | --- |
| Compose 런타임 | 세 서비스는 `docker-compose.yml`에 정의되어 있고, N100 override는 loopback `8001`/`8002`/`8003`, 단일 Uvicorn worker, `/health` healthcheck를 유지한다. | Compose가 현재 선언된 런타임이며 K3s workload가 이를 대체하도록 선언되어 있지 않다. |
| 영속 데이터 | crawler는 `/data/crawler-worker`의 SQLite DB와 JSON archive를 쓴다. YouTube와 Book은 각각 `/data/youtube-memo/youtube_memo.sqlite3`, `/data/book-memo/book_memo.sqlite3`을 쓰며 rate-limit state 기본 경로도 DB 부모이다. | 세 서비스 모두 writable local data가 있고 memo 둘은 SQLite writer, crawler는 archive writer와 lifespan background scheduler를 가진다. |
| K3s 초안 | scope/storage template은 세 서비스를 1차 후보와 `local-path` PVC 후보로 기록하지만 capacity는 placeholder다. 루트 Kustomize는 `resources: []`이고 앱 template 디렉터리는 `portal-web`뿐이다. | 세 서비스의 Deployment, Service, PVC resource contract는 아직 없다. |
| 전환 실행기 | policy는 서비스·PVC·phase allowlist를 가지지만 image는 반복 문자 digest placeholder다. runner는 lock 후 phase를 journal에 기록할 뿐 backup, copy, start, health verification을 구현하지 않는다. | 실제 cutover 실행기가 아니라 승인된 native runtime이 채워야 할 release-shape contract다. |
| 검증 | crawler scheduler/routes, YouTube UI, Book service tests와 inactive draft를 읽는 `test_k8s_storage_draft.py`가 있다. | 서비스 회귀와 일부 K3s draft 규칙은 검사되지만 세 서비스 workload/PVC mapping test는 없다. |

## 선택한 설계

다음 구현은 **inactive workload-contract layer**만 추가한다. 서비스별 `.yaml.tmpl`은 replica-zero Deployment, ClusterIP Service, RWO `local-path` PVC contract를 가진다. image와 capacity는 `__CONFIRM_*__` placeholder이고 Secret은 service-specific 이름만 `envFrom.secretRef`로 참조한다. Secret 값·key·token·password는 Git에 기록하지 않는다.

| 서비스 | port·health | data mount와 명시 env | Secret/PVC reference |
| --- | --- | --- | --- |
| `crawler-worker` | `8001`, `/health` | `/data/crawler-worker`; `NEWS_DB_PATH=/data/crawler-worker/news_summaries.sqlite3`; `NEWS_ARCHIVE_PATH=/data/crawler-worker/news_archive.json` | `crawler-worker-runtime`; `crawler-worker-data-dynamic-draft` |
| `youtube-memo` | `8002`, `/health` | `/data/youtube-memo`; `YOUTUBE_MEMO_DB_PATH=/data/youtube-memo/youtube_memo.sqlite3` | `youtube-memo-runtime`; `youtube-memo-data-dynamic-draft` |
| `book-memo` | `8003`, `/health` | `/data/book-memo`; `BOOK_MEMO_DB_PATH=/data/book-memo/book_memo.sqlite3` | `book-memo-runtime`; `book-memo-data-dynamic-draft` |

Every artifact says `DRAFT ONLY — NOT FOR APPLY`, uses the existing draft annotation convention, and remains unreachable from active Kustomize. ClusterIP is selected to avoid prematurely selecting a Caddy-facing NodePort. The change does not modify `crawler-worker/app/services/news_scheduler.py`; the crawler must remain a one-replica candidate because its FastAPI lifespan starts an in-process collector.

## Invariants and validation

- Each data set has one writer only: current Compose **or** a future K3s workload, never both.
- New Deployment templates stay at zero replicas and root Kustomize stays empty, so normal repository rendering creates no writer.
- SQLite files (including sidecars), crawler archive, and memo rate-limit state move only as an approved service-specific data set. No host/source path is embedded in a template.
- Capacity, UID/GID, actual immutable digest, backup/restore evidence, and SQLite `PRAGMA quick_check` are maintenance-window measurements, never guessed values.
- A standard-library unittest, following `test_k8s_storage_draft.py`, proves the exact port/mount/env mapping and rejects NodePort, Ingress, `hostPath`, `nodeAffinity`, active Kustomize references, concrete capacity, and nonzero replicas.
- Existing focused service tests protect unchanged behavior. Kubernetes API, image build, Compose, scheduler, network, and deployment tests are excluded from this code-only increment.

## Alternatives

1. Applying manifests or using the runner now is rejected: real data, ownership, digest, capacity, Secret injection and restore evidence are unverified, and the runner does not cut over a service.
2. Changing Compose, Caddy, deployment scripts, or crawler scheduling is rejected: it changes runtime behavior and exceeds the approved boundary.
3. Inactive contracts plus static tests is selected: it fills the missing repository declaration without changing the Compose runtime.

## Operational approval boundary

Separate explicit N100 operating approval is required to measure data and ownership; choose capacities and immutable built image digests; create and independently restore encrypted backup evidence; run `quick_check` with the Compose writer stopped; seed Secrets; create/bind PVCs; start and privately verify one K3s workload at a time; operate a real transition runner; or change NodePort/Caddy/tunnel/public traffic. Caddy, Cloudflare/tunnel, deployment automation, and scheduler changes are not authorized by this design.
