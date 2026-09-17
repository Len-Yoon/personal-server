# Cloudflare Tunnel 운영 가이드

현재 공개 경로는 **Cloudflare Tunnel → WSL loopback Caddy → K3s Portal** 및 **Cloudflare Tunnel → WSL loopback Caddy → K3s Book Memo**와 Compose 서비스의 Tunnel 직접 ingress로 나뉨. Hyundai OAuth callback인 `car.len.pe.kr`도 Caddy 경로에 포함하지 않음. 공유기 포트포워딩은 이 경로에 사용하지 않음.

## 현재 ingress 기준

2026-09-16 N100에서 `cloudflared-personal-server.service`가 active이고 9개 ingress가 있는 것을 읽기 전용으로 대조함. Tunnel ID, 인증 파일, private IP는 문서에 기록하지 않음.

| 호스트 | Tunnel 대상 유형 | 최종 대상 |
|---|---|---|
| `len.pe.kr`, `portal.len.pe.kr`, `file.len.pe.kr`, `admin.len.pe.kr`, `portfolio.len.pe.kr` | WSL loopback Caddy | K3s `portal-web` NodePort |
| `news.len.pe.kr` | Compose `crawler-worker` loopback endpoint | `crawler-worker` |
| `memo.len.pe.kr` | Compose `youtube-memo` loopback endpoint | `youtube-memo` |
| `books.len.pe.kr` | WSL loopback Caddy | K3s `book-memo` Service |
| `car.len.pe.kr` | `car-care-worker` 비공개 callback upstream | Hyundai OAuth callback |

뉴스·YouTube 메모는 Caddy를 거치지 않고 각각의 Compose loopback endpoint로 연결됨. Book Memo는 Caddy를 거쳐 K3s `book-memo` Service로 연결됨. Caddy 경유 여부나 private callback upstream은 Tunnel 설정 대조 결과이며, 주소 값 자체는 운영 문서에 기록하지 않음.

## 차량 OAuth callback ingress

`Caddyfile`에는 `car.len.pe.kr` 호스트 블록이 없음. 따라서 Hyundai OAuth callback은 Caddy 대상 표에 추가하지 않으며, Cloudflare Tunnel의 별도 ingress가 `car-care-worker` 비공개 callback upstream으로 전달함. private callback upstream의 주소는 저장소·문서·로그에 기록하지 않음.

이 Tunnel 설정은 저장소 밖의 `~/.cloudflared/config.yml`에 있으므로 실제 운영 값은 변경 전 읽기 전용 대조가 필요함. 차량 callback ingress가 없거나 Caddy로 잘못 전달되면 `HYUNDAI_REDIRECT_URI`의 callback이 404가 될 수 있음.

Tunnel은 Windows 로그인 뒤 WSL 사용자 서비스로 실행됨. `PersonalServer-WSL-KeepAlive`가 WSL을 유지하고, `personal-server-autostart`의 Supervisor가 Daemon을 단일 관리함. Supervisor는 초기 120초 대기 뒤 Daemon을 시작하며, Daemon은 3분 간격으로 WSL·K3s·Portal·NodePort·Tunnel을 점검함. Daemon이 비정상 종료되면 Supervisor가 15초 뒤 재기동하고, 짧은 시간에 3회 연속 종료되면 60초 backoff를 적용함. 같은 구성요소가 2회 연속 비정상이면 승인된 제한 복구를 시도하며, 구성요소별 시도는 최대 3회임. 복구 상태를 기록하지 못하면 중복·무한 복구를 막기 위해 추가 복구를 중단함. SSH 종료만으로 Tunnel이 내려가면 안 됨. Tunnel 단독 장애에는 호스트 긴급 재부팅을 사용하지 않음.

## 정상 확인

```bash
curl --fail --silent --show-error https://len.pe.kr/health
systemctl --user status cloudflared-personal-server.service --no-pager
```

`/health`가 정상이고 Tunnel 서비스가 active면 공개 경로는 정상임.

## Cloudflare 1033 또는 502 대응

| 화면 | 의미 | 먼저 할 일 |
|---|---|---|
| Cloudflare 1033 | Tunnel 연결을 찾지 못함 | WSL 유지 상태와 `cloudflared-personal-server.service` 확인 |
| Cloudflare 502 | Tunnel은 연결됐지만 내부 대상 응답 실패 | Caddy, K3s `portal-web` 또는 `book-memo` Service 상태 확인 |

N100에서 다음 순서로 확인함.

```bash
systemctl --user is-active cloudflared-personal-server.service
sudo k3s kubectl -n personal-server get deploy,pod
curl --resolve len.pe.kr:443:127.0.0.1 --fail --silent --show-error https://len.pe.kr/health
```

수동 rollback이 필요한 경우 서비스가 inactive면 아래 시작 명령을 사용함. 서비스는 active지만 Tunnel 연결 프로세스가 없으면 `start` 대신 `restart`를 사용함. 호스트 재부팅이나 다른 서비스 재생성은 수행하지 않음.

```bash
systemctl --user start cloudflared-personal-server.service
```

```bash
systemctl --user restart cloudflared-personal-server.service
```

N100 감시기는 Tunnel 장애의 최초 전환에 Telegram 장애 알림을 1회 시도함. 전송에 성공한 상태에서만 이후 정상 복구 전환을 Telegram으로 1회 알림하며, 전송에 실패하면 다음 점검에서 장애 알림을 재시도함. Telegram 자격 증명은 운영자가 Windows 자격 증명 관리자에 사전 등록하며, 값은 문서·로그·상태 파일에 기록하지 않음. 외부 상태는 GitHub Actions가 약 5분 간격으로 독립 점검하므로 같은 장애에 대한 알림이 중복될 수 있음. N100 전원·네트워크·WSL 자체가 동작하지 않는 경우에는 N100 직접 알림과 복구를 보장할 수 없음. 상세는 [공개 상태 Telegram 알림](public-uptime-monitor.md)을 참고함.
