# 운영 참조

현재 N100 개인 서버의 서비스 구조와 일상 점검 기준 문서임. 이 서버는 서비스 실행뿐 아니라 관측성, 장애 감지, 복구, 백업·복원 검증, 알림을 연결한 SRE 운영 체계로 관리함. 비밀값은 `.env`, Kubernetes Secret, GitHub Secret에만 보관하며 이 문서·명령 출력에 기록하지 않음.

## 현재 런타임

**Cloudflare Tunnel → Caddy → K3s Portal** 경로로 공개 Portal을 제공함. 뉴스·YouTube 메모·책 메모는 Tunnel 직접 ingress로 각 Docker Compose 서비스에 연결되며, 차량 OAuth callback은 별도 비공개 upstream을 사용함. Caddy는 K3s NodePort의 `portal-web`로만 전달함.

| 구성 | 실행 위치 | 상태 데이터 |
|---|---|---|
| `portal-web` | K3s `personal-server` namespace | `portal-web-files-dynamic`, `portal-web-state-dynamic` PVC |
| `crawler-worker` | Docker Compose | Compose 데이터 경로 |
| `youtube-memo` | Docker Compose | Compose 데이터 경로 |
| `book-memo` | Docker Compose | Compose 데이터 경로 |
| `system-agent`, `homeops-executor` | Docker Compose 내부 경계 | 호스트 상태·제한형 운영 작업 |
| `car-care-worker` | Docker Compose | 차량관리 데이터·Telegram |
| Prometheus·Grafana·Alertmanager relay | K3s `monitoring` namespace | monitoring PVC와 Kubernetes Secret |

## 컨테이너 실행 권한

`caddy`, `car-care-worker`, `homeops-executor`, `portal-web`, `system-agent`는 UID/GID `10001:10001`의 전용 계정으로 실행함. Caddy는 내부 80·443 포트 binding에 필요한 `NET_BIND_SERVICE` capability만 사용함.

Portal PVC는 `portal-web-files-dynamic`, `portal-web-state-dynamic` 두 개이며 K3s 단일 writer만 연결함. Portal non-root 이미지 교체 전에는 두 PVC의 UID/GID `10001:10001` 읽기·쓰기·디렉터리 접근 권한을 확인함. 권한 불충족 시 Deployment를 변경하지 않으며, PVC 권한 정렬은 자동화하지 않고 별도 운영 승인 아래 최소 범위로 수행함.

## 공개 도메인

| 도메인 | 현재 대상 |
|---|---|
| `len.pe.kr`, `portal.len.pe.kr`, `file.len.pe.kr`, `admin.len.pe.kr`, `portfolio.len.pe.kr` | K3s `portal-web` |
| `news.len.pe.kr` | `crawler-worker` |
| `memo.len.pe.kr` | `youtube-memo` |
| `books.len.pe.kr` | `book-memo` |
| `car.len.pe.kr` | `car-care-worker` OAuth callback. Caddy가 아닌 별도 Cloudflare Tunnel ingress의 비공개 callback upstream |

## 일상 상태 확인

N100 WSL에서 실행함.

```bash
cd /mnt/c/personal-server
curl --fail --silent --show-error https://len.pe.kr/health
sudo k3s kubectl -n personal-server get deploy,pod,pvc
bash infra/k8s/tools/sre-health-audit.sh
```

Grafana, Prometheus, Telegram relay, Portal PVC 백업은 [K3s 운영 문서](../infra/k8s/README.md)를 따름.

## 장애 알림과 복구

| 신호 | 감지 방식 | 알림 |
|---|---|---|
| 공개 주소 장애 | GitHub Actions 약 5분 간격 health 점검 | Telegram 장애·복구 전환 시 1회. N100 알림과 중복 가능 |
| N100 기반 구성요소 이상 | `personal-server-autostart`의 Supervisor가 Daemon을 단일 관리하고, Daemon이 3분 간격으로 WSL·K3s·Portal·NodePort·Tunnel 점검 | Tunnel 장애 알림 전송 성공 뒤 정상 복구 전환 시 Telegram 각 1회 |
| K3s·노드·워크로드 이상 | Prometheus Alertmanager → SRE relay | Telegram |
| Portal PVC 백업·복원 검증 | N100 사용자 timer | Telegram |
| Compose 컨테이너 이상 | HomeOps 진단·제한형 복구 | 관리자 상태·필요 시 Telegram |
| 뉴스 수집 지연·연속 실패 | Prometheus `NewsCollectionStale` | SRE relay → Telegram |

N100 자동복구는 Windows 시작 시 Supervisor를 하나만 실행하고, 초기 120초 뒤 Daemon을 하나만 시작함. Daemon이 비정상 종료되면 Supervisor가 15초 뒤 재기동하며, 60초 안에 3회 연속 종료되면 60초 backoff를 적용함. Tunnel은 NodePort·서비스·프로세스·공개 health를 함께 확인하고, NodePort 장애는 Tunnel 알림·복구로 오분류하지 않음. 같은 구성요소가 2회 연속 비정상일 때만 승인된 제한 복구를 시도하고, 구성요소별 시도 횟수를 최대 3회로 제한함. Supervisor의 host metrics 기록 실패는 감시 기동을 막지 않지만, 상태 기록을 저장하지 못하면 중복·무한 복구를 막기 위해 추가 복구를 중단함. K3s가 이미 실행 중이면 재시작하지 않으며, Portal PVC·Secret·운영 데이터·Caddy·Tunnel ingress는 수정하지 않음.

N100 감시기는 Tunnel 장애의 최초 전환에 직접 Telegram 장애 알림을 1회 시도함. 전송이 성공한 상태에서만 정상 복구 전환을 Telegram으로 1회 알리며, 전송 실패 시 다음 점검에서 장애 알림을 재시도함. GitHub Actions의 외부 점검은 독립 보완 경로이므로 같은 장애에 대한 Telegram 알림은 중복될 수 있음. N100 전원·네트워크·WSL 자체가 동작하지 않는 경우에는 N100 감시·직접 알림·자동복구를 보장할 수 없음. Telegram 자격 증명은 Windows 자격 증명 관리자에만 보관하며 문서·로그·상태 파일에는 기록하지 않음.

공개 상태 Telegram 알림의 Secret 설정과 수동 점검은 [공개 상태 Telegram 알림](public-uptime-monitor.md)을 따름.

HomeOps 실행기는 Docker socket을 제한된 allowlist 진단·재시작에만 사용함. Portal 관리자 비밀번호는 실행기 통신에 재사용하지 않으며, 운영자가 사전 설정한 `HOMEOPS_EXECUTOR_SHARED_SECRET`이 비어 있으면 Portal과 실행기 모두 HomeOps 요청을 fail-closed 처리함. 값은 Git·문서·로그·상태 파일에 기록하지 않음.

## 운영 경계

- Portal은 K3s 단일 writer로만 실행함. Compose Portal과 동시에 실행하지 않음.
- K3s Portal 전환·rollback·PVC 작업은 `infra/k8s/tools/portal-cutover.sh`의 명시적 운영 절차만 사용함.
- 자동 배포는 `crawler-worker`, `youtube-memo`, `book-memo`, `car-care-worker`의 허용된 Compose 변경만 처리함.
- Caddy, Cloudflare Tunnel, K3s Secret·PVC, `.env`, `data/`, Portal은 자동 배포에서 제외함.
- Trivy filesystem/config 검사는 HIGH·CRITICAL 결과를 CI 차단 기준으로 사용함.

## Book Memo K3s 전환 승인·검증

Book Memo 전환 도구는 서비스별 사용자 승인 후 운영자가 수동으로 실행함. 정적 `book-memo.yaml`은 실행 불가 sentinel 이미지와 replica 0만 포함하므로 직접 적용하지 않음. 운영 적용·공개 경로 변경·자동 실행 활성화를 저장소 구현 완료와 구분하여 보고함.

준비·전환 순서는 다음과 같음.

1. 검증된 Linux AMD64 OCI 이미지를 `k3s-app-image-import.sh --go`로 반입하고, 성공 출력의 canonical digest 별칭을 다음 단계 입력으로 사용함.
2. 운영자가 기존 런타임 설정과 동일한 `book-memo-runtime` Secret을 사전 시딩함. Secret 값은 조회·출력·복제하지 않음.
3. 별도 승인 후 `book-memo-prepare.sh --go --image docker.io/library/personal-server-book-memo@sha256:<digest>`를 실행함. 이 도구만 importer가 등록·검증한 canonical immutable digest 별칭을 sentinel에 메모리에서 치환하고 PVC·Deployment·Service를 replica 0으로 생성함.
4. `book-memo-cutover.sh --check`으로 writer 부재·PVC·이미지·Deployment 계약을 읽기 전용 검증함.
5. 별도 전환 승인 후에만 `book-memo-cutover.sh --go`를 실행함.

준비 도구는 Docker·원본 데이터·Secret 값을 읽거나 변경하지 않으며 replica를 1로 올리지 않음. 기존 PVC·Deployment·Service 중 하나라도 있으면 중단함.

| 모드 | 승인·수행 범위 | 검증·실패 처리 | 비고 |
|---|---|---|---|
| `--check` | 읽기 전용 사전 점검 | Compose health, K3s writer 부재, 반입된 digest 고정 AMD64 이미지, Bound PVC, Deployment 계약 확인 | 데이터 복사 없음 |
| `--prepare` | 사전 점검 및 Kubernetes server dry-run | 실제 리소스를 생성하지 않음 | `book-memo-prepare.sh --go` 적용 이후에만 사용 |
| `--go` | 별도 전환 승인 후 Compose 중지, 빈 PVC로 1회 복사, K3s writer 기동 | Compose 중지 재확인, 양쪽 SQLite `PRAGMA quick_check`, 전체 데이터 digest 일치, rollout 확인 | 실패 시 K3s 종료 확인 후 Compose 복구 |
| `--rollback` | 별도 롤백 승인 후 K3s 중지 및 Compose 복귀 | 양쪽 데이터가 같을 때만 복귀함. 데이터가 달라졌으면 K3s를 복구하고 실패함 | 신규 쓰기 이후 역방향 데이터 복사는 별도 승인 필요 |

네 가지 모드는 상호 배타적이며 중복 지정도 거부함. `--go`만 정방향 전환을 승인하며 `--rollback`은 독립적인 롤백 승인임. 모든 모드에서 `--source`, `--database`, `--image` 입력을 검증하며, 실제 데이터 위치와 이미지 digest 값은 문서·Git·로그에 기록하지 않음. Secret 키 이름은 `BOOK_MEMO_DB_PATH`, `ALADIN_TTB_KEY`, `DELETE_PASSWORD`, `APP_ENV`, `AUTH_RATE_LIMIT_STATE_PATH`이며 값은 사전 시딩 절차로만 관리함. 운영자는 DB 설정과 검증 대상의 일치 여부 및 runtime 쓰기 권한을 별도 확인함.

전환·롤백 동안 자동 복구·배포·다른 운영자의 writer 재기동이 개입하지 않는 유지보수 조건을 사전에 확인함. 전환 도구는 임시 데이터 검증 Pod만 생성·삭제하며 Secret을 조회·변경하지 않음. 복사 대상이 비어 있지 않으면 덮어쓰지 않음. 실패한 복사의 잔여 데이터는 보존하며 재시도 전 별도 검토가 필요함. `recovery_required` 실패 단계가 보고되면 자동 재실행하지 않고 writer 상태와 복구 가능 여부를 확인함.

공개 경로는 별도 승인·검증 대상이며 이 도구의 성공은 공개 서비스 전환 완료를 의미하지 않음. Secret·Portal·Caddy·Tunnel은 변경하지 않음. 적용 전후 독립 운영 검토, 서비스 수동 검증, 외부 health 검증은 실제 적용 승인 후 별도로 수행함.

## 뉴스 수집 관측성

`crawler-worker`는 수집 상태를 `/data/crawler-worker/news_collection_status.json`에 원자적으로 저장함. 상태 파일에는 시각과 실패 횟수만 기록되며 기사·URL·예외 원문·토큰은 포함하지 않음.

Prometheus 수집은 `infra/k8s/sre-telegram/crawler-news-observability.yaml`의 `ServiceMonitor`를 별도 승인 후 적용함. 이 `ServiceMonitor`는 Portal cutover가 만든 `portal-compose-bridge` 라벨의 `compose-crawler` Service와 EndpointSlice를 전제함. 인증은 Secret의 `bearer_token` 키를 참조하며 값은 문서·Git에 기록하지 않음. `/internal/metrics`는 정확한 Bearer 인증이 없으면 404를 반환함.

시딩 계약은 monitoring namespace Secret `crawler-news-metrics`의 `bearer_token` 키와 crawler runtime의 `NEWS_METRICS_BEARER_TOKEN`에 동일한 승인된 값을 주입하는 것임. 값 자체는 이 문서·Git·로그에 기록하지 않음. 적용 대상은 새 `crawler-news-observability.yaml`의 `ServiceMonitor`와 갱신된 `prometheus-rule.yaml`임.

검증 순서:

1. crawler 상태 파일의 최근 시도·성공·실패 횟수를 확인함.
2. 인증된 내부 metrics 응답에 고정 지표만 노출되는지 확인함.
3. Prometheus에서 `NewsCollectionStale`의 수집·평가 상태를 확인함.
4. Alertmanager → SRE relay의 장애·복구 한국어 메시지를 확인함.

적용 전후 검증 명령은 다음 리소스와 키만 확인하며 Secret 값은 출력하지 않음.

```bash
kubectl -n monitoring get secret crawler-news-metrics -o jsonpath='{.data.bearer_token}' >/dev/null
kubectl -n personal-server get service compose-crawler -l app.kubernetes.io/part-of=portal-compose-bridge
kubectl -n personal-server get endpointslice -l app.kubernetes.io/part-of=portal-compose-bridge
kubectl -n monitoring apply --dry-run=client -f infra/k8s/sre-telegram/crawler-news-observability.yaml >/dev/null
kubectl -n monitoring apply --dry-run=client -f infra/k8s/sre-telegram/prometheus-rule.yaml >/dev/null
kubectl -n monitoring get servicemonitor crawler-news-observability
kubectl -n monitoring get prometheusrule sre-telegram-k3s-alerts
kubectl -n monitoring get servicemonitor crawler-news-observability -o yaml | rg 'compose-crawler|internal/metrics|bearerTokenSecret'
```

별도 승인 적용 절차:

```bash
kubectl -n monitoring apply -f infra/k8s/sre-telegram/crawler-news-observability.yaml
kubectl -n monitoring apply -f infra/k8s/sre-telegram/prometheus-rule.yaml
kubectl -n monitoring get servicemonitor crawler-news-observability
kubectl -n monitoring get prometheus -o name
```

적용 전 Secret key와 `portal-compose-bridge`의 `compose-crawler` Service·EndpointSlice 존재 여부만 확인하고 값은 출력하지 않음. bridge 리소스가 없으면 ServiceMonitor를 적용하지 않고 Portal cutover 상태를 먼저 확인함. 적용 후 `ServiceMonitor` 상태와 Prometheus target의 `compose-crawler` 및 `/internal/metrics` 수집 상태를 확인함.

롤백 시 crawler 직전 이미지를 복귀한 뒤 `ServiceMonitor`와 `NewsCollectionStale` 규칙만 제거함. 실제 적용·Secret 생성·배포는 별도 승인 필요함.

```bash
kubectl -n monitoring delete servicemonitor crawler-news-observability
kubectl -n monitoring patch prometheusrule sre-telegram-k3s-alerts --type=json -p='[{"op":"remove","path":"/spec/groups/0/rules/5"}]'
```

## Portal HTTP 관측성

Portal은 `/internal/metrics`에서 요청 수와 요청 지연시간 histogram만 Prometheus 형식으로 제공함. 지표 label은 `method`, 정규화된 route template, `status_code`만 사용함. raw path, query string, IP, 사용자 식별자, 인증정보, 요청·응답 본문, 예외 원문은 지표에 기록하지 않음.

`/internal/metrics`는 `PORTAL_METRICS_BEARER_TOKEN`과 일치하는 Bearer 인증이 없는 경우 404를 반환함. Portal NodePort는 Caddy를 통해 공개 경로로 전달될 수 있으므로, 내부 경로명만으로 비공개 경계가 성립한다고 간주하지 않음.

Prometheus 수집은 `infra/k8s/sre-telegram/portal-http-observability.yaml`의 `ServiceMonitor`를 별도 승인 후 적용함. 이 리소스는 `personal-server` namespace의 `app.kubernetes.io/name=portal-web` Service와 `http` port만 선택함. 인증은 monitoring namespace Secret `portal-http-metrics`의 `bearer_token` 키를 참조하며, 값은 문서·Git·로그에 기록하지 않음.

Kubernetes Secret은 namespace 간 공유되지 않음. 따라서 동일한 승인된 bearer 값은 다음 두 Secret에 운영자 절차로 각각 시딩해야 함.

| Namespace | Secret | Key | 사용처 |
|---|---|---|---|
| `monitoring` | `portal-http-metrics` | `bearer_token` | ServiceMonitor의 Prometheus scrape 인증 |
| `personal-server` | `portal-http-metrics` | `bearer_token` | Portal Pod의 `PORTAL_METRICS_BEARER_TOKEN` 환경변수 |

적용 전에는 두 namespace의 Secret key 존재 여부와 ServiceMonitor dry-run만 수행함. Portal Deployment에는 `personal-server/portal-http-metrics`의 `bearer_token`을 `PORTAL_METRICS_BEARER_TOKEN`으로 참조하는 환경변수만 추가함. 기존 `portal-web-runtime` Secret, Portal cutover, Caddy, Tunnel 변경은 별도 승인 범위임.

```bash
kubectl -n monitoring get secret portal-http-metrics -o jsonpath='{.data.bearer_token}' >/dev/null
kubectl -n personal-server get secret portal-http-metrics -o jsonpath='{.data.bearer_token}' >/dev/null
kubectl -n personal-server get service portal-web -l app.kubernetes.io/name=portal-web
kubectl -n monitoring apply --dry-run=client -f infra/k8s/sre-telegram/portal-http-observability.yaml >/dev/null
```

적용 후에는 일반 Portal 요청을 최소 1회 발생시킨 뒤 Prometheus target의 `portal-web` 수집 상태와 `portal_http_requests_total`, `portal_http_request_duration_seconds` 지표의 존재를 확인함. 초기 요청 전에는 HELP·TYPE 선언만 존재할 수 있음. 값에 인증정보 또는 식별 정보가 포함되면 즉시 적용을 중지하고 외부 노출 없이 원인을 검토함.
