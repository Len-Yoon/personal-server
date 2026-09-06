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

짧은 재부팅처럼 외부 점검 사이에 복구되는 경우 공개 장애 메시지는 발송되지 않음. 이는 장애 전환을 확인한 경우에만 알리는 의도된 동작임.

공개 상태 Telegram 알림의 Secret 설정과 수동 점검은 [공개 상태 Telegram 알림](public-uptime-monitor.md)을 따름.

## 운영 경계

- Portal은 K3s 단일 writer로만 실행함. Compose Portal과 동시에 실행하지 않음.
- K3s Portal 전환·rollback·PVC 작업은 `infra/k8s/tools/portal-cutover.sh`의 명시적 운영 절차만 사용함.
- 자동 배포는 `crawler-worker`, `youtube-memo`, `book-memo`, `car-care-worker`의 허용된 Compose 변경만 처리함.
- Caddy, Cloudflare Tunnel, K3s Secret·PVC, `.env`, `data/`, Portal은 자동 배포에서 제외함.
