# 운영 참조

현재 N100 개인 서버의 서비스 구조와 일상 점검 기준 문서임. 이 서버는 서비스 실행뿐 아니라 관측성, 장애 감지, 복구, 백업·복원 검증, 알림을 연결한 SRE 운영 체계로 관리함. 비밀값은 `.env`, Kubernetes Secret, GitHub Secret에만 보관하며 이 문서·명령 출력에 기록하지 않음.

## 현재 런타임

**Cloudflare Tunnel → Caddy → K3s Portal** 경로로 공개 Portal을 제공함. Caddy는 K3s NodePort의 `portal-web`로 전달하고, 나머지 업무 서비스는 Docker Compose 컨테이너로 유지함.

| 구성 | 실행 위치 | 상태 데이터 |
|---|---|---|
| `portal-web` | K3s `personal-server` namespace | `portal-web-files-dynamic`, `portal-web-state-dynamic` PVC |
| `crawler-worker` | Docker Compose | Compose 데이터 경로 |
| `youtube-memo` | Docker Compose | Compose 데이터 경로 |
| `book-memo` | Docker Compose | Compose 데이터 경로 |
| `system-agent`, `homeops-executor` | Docker Compose 내부 경계 | 호스트 상태·제한형 운영 작업 |
| `car-care-worker` | Docker Compose | 차량관리 데이터·Telegram |
| Prometheus·Grafana·Alertmanager relay | K3s `monitoring` namespace | monitoring PVC와 Kubernetes Secret |

## 공개 도메인

| 도메인 | 현재 대상 |
|---|---|
| `len.pe.kr`, `portal.len.pe.kr`, `file.len.pe.kr`, `admin.len.pe.kr`, `portfolio.len.pe.kr` | K3s `portal-web` |
| `news.len.pe.kr` | `crawler-worker` |
| `memo.len.pe.kr` | `youtube-memo` |
| `books.len.pe.kr` | `book-memo` |
| `car.len.pe.kr` | `car-care-worker` OAuth callback |

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
| 공개 주소 장애 | GitHub Actions 약 5분 간격 health 점검 | Telegram 장애·복구 전환 시 1회 |
| K3s·노드·워크로드 이상 | Prometheus Alertmanager → SRE relay | Telegram |
| Portal PVC 백업·복원 검증 | N100 사용자 timer | Telegram |
| Compose 컨테이너 이상 | HomeOps 진단·제한형 복구 | 관리자 상태·필요 시 Telegram |
| 뉴스 수집 지연·연속 실패 | Prometheus `NewsCollectionStale` | SRE relay → Telegram |

짧은 재부팅처럼 외부 점검 사이에 복구되는 경우 공개 장애 메시지는 발송되지 않음. 이는 장애 전환을 확인한 경우에만 알리는 의도된 동작임.

공개 상태 Telegram 알림의 Secret 설정과 수동 점검은 [공개 상태 Telegram 알림](public-uptime-monitor.md)을 따름.

## 운영 경계

- Portal은 K3s 단일 writer로만 실행함. Compose Portal과 동시에 실행하지 않음.
- K3s Portal 전환·rollback·PVC 작업은 `infra/k8s/tools/portal-cutover.sh`의 명시적 운영 절차만 사용함.
- 자동 배포는 `crawler-worker`, `youtube-memo`, `book-memo`, `car-care-worker`의 허용된 Compose 변경만 처리함.
- Caddy, Cloudflare Tunnel, K3s Secret·PVC, `.env`, `data/`, Portal은 자동 배포에서 제외함.

## 뉴스 수집 관측성

`crawler-worker`는 수집 상태를 `/data/crawler-worker/news_collection_status.json`에 원자적으로 저장함. 상태 파일에는 시각과 실패 횟수만 기록되며 기사·URL·예외 원문·토큰은 포함하지 않음.

Prometheus 수집은 `infra/k8s/sre-telegram/crawler-news-observability.yaml`의 `ServiceMonitor`를 별도 승인 후 적용함. 인증은 Secret의 `bearer_token` 키를 참조하며 값은 문서·Git에 기록하지 않음. `/internal/metrics`는 정확한 Bearer 인증이 없으면 404를 반환함.

시딩 계약은 monitoring namespace Secret `crawler-news-metrics`의 `bearer_token` 키와 crawler runtime의 `NEWS_METRICS_BEARER_TOKEN`에 동일한 승인된 값을 주입하는 것임. 값 자체는 이 문서·Git·로그에 기록하지 않음. 적용 대상은 새 `crawler-news-observability.yaml`의 `ServiceMonitor`와 갱신된 `prometheus-rule.yaml`임.

검증 순서:

1. crawler 상태 파일의 최근 시도·성공·실패 횟수를 확인함.
2. 인증된 내부 metrics 응답에 고정 지표만 노출되는지 확인함.
3. Prometheus에서 `NewsCollectionStale`의 수집·평가 상태를 확인함.
4. Alertmanager → SRE relay의 장애·복구 한국어 메시지를 확인함.

적용 전후 검증 명령은 다음 리소스와 키만 확인하며 Secret 값은 출력하지 않음.

```bash
kubectl -n monitoring get secret crawler-news-metrics -o jsonpath='{.data.bearer_token}' >/dev/null
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

적용 전 Secret key 존재 여부만 확인하고 값은 출력하지 않음. 적용 후 `ServiceMonitor` 상태와 Prometheus target의 `compose-crawler` 및 `/internal/metrics` 수집 상태를 확인함.

롤백 시 crawler 직전 이미지를 복귀한 뒤 `ServiceMonitor`와 `NewsCollectionStale` 규칙만 제거함. 실제 적용·Secret 생성·배포는 별도 승인 필요함.

```bash
kubectl -n monitoring delete servicemonitor crawler-news-observability
kubectl -n monitoring patch prometheusrule sre-telegram-k3s-alerts --type=json -p='[{"op":"remove","path":"/spec/groups/0/rules/5"}]'
```
