# Cloudflare Tunnel 운영 가이드

현재 공개 경로는 **Cloudflare Tunnel → WSL localhost Caddy → K3s Portal 또는 Compose 서비스**임. 단, Hyundai OAuth callback인 `car.len.pe.kr`은 Caddy 경로에 포함하지 않으며 별도 Tunnel ingress로 `car-care-worker` loopback endpoint에 연결해야 함. 공유기 포트포워딩은 이 경로에 사용하지 않음.

## 현재 ingress 기준

Tunnel의 `~/.cloudflared/config.yml`은 아래 Caddy 대상 호스트를 WSL의 `https://localhost:443`으로 전달함. Caddy가 host 이름을 기준으로 다음 대상으로 분기함.

| 호스트 | Caddy 대상 |
|---|---|
| `len.pe.kr`, `portal.len.pe.kr`, `file.len.pe.kr`, `admin.len.pe.kr`, `portfolio.len.pe.kr` | K3s `portal-web` NodePort |
| `news.len.pe.kr` | Compose `crawler-worker` |
| `memo.len.pe.kr` | Compose `youtube-memo` |
| `books.len.pe.kr` | Compose `book-memo` |

## 차량 OAuth callback ingress

`Caddyfile`에는 `car.len.pe.kr` 호스트 블록이 없음. 따라서 Hyundai OAuth callback은 Caddy 대상 표에 추가하지 않으며, Cloudflare Tunnel의 별도 ingress가 `http://localhost:8015`의 `car-care-worker` callback으로 전달해야 함.

이 Tunnel 설정은 저장소 밖의 `~/.cloudflared/config.yml`에 있으므로 실제 운영 값은 별도 확인 필요함. 설정이 없거나 Caddy 443으로 전달되면 `HYUNDAI_REDIRECT_URI`의 callback이 404가 될 수 있음.

Tunnel은 Windows 로그인 뒤 WSL 사용자 서비스로 실행됨. `PersonalServer-WSL-KeepAlive`가 WSL을 유지하고, `personal-server-autostart`가 3분 간격으로 Tunnel 프로세스와 공개 경로의 기반 구성요소를 점검함. 같은 항목이 2회 연속 비정상이면 Tunnel 사용자 서비스 시작·재시작을 포함한 제한된 복구를 시도하며, 항목별 시도는 최대 3회임. SSH 종료만으로 Tunnel이 내려가면 안 됨. Tunnel 단독 장애에는 호스트 긴급 재부팅을 사용하지 않음.

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
| Cloudflare 502 | Tunnel은 연결됐지만 내부 대상 응답 실패 | Caddy, K3s `portal-web`, NodePort 상태 확인 |

N100에서 다음 순서로 확인함.

```bash
systemctl --user is-active cloudflared-personal-server.service
sudo k3s kubectl -n personal-server get deploy,pod
curl --resolve len.pe.kr:443:127.0.0.1 --fail --silent --show-error https://len.pe.kr/health
```

수동 rollback이 필요한 경우 아래 사용자 서비스 시작 명령만 사용함. 호스트 재부팅이나 다른 서비스 재생성은 수행하지 않음.

```bash
systemctl --user start cloudflared-personal-server.service
```

자동복구 작업은 Telegram을 직접 발송하지 않음. 외부 상태는 GitHub Actions가 약 5분 간격으로 별도 점검하며, 장애 알림 전송에 성공한 뒤 이후 정상 전환을 확인하면 Telegram 복구 알림을 보냄. 상세는 [공개 상태 Telegram 알림](public-uptime-monitor.md)을 참고함.
