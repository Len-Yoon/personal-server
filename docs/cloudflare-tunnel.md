# Cloudflare Tunnel 운영 가이드

현재 공개 경로는 **Cloudflare Tunnel → WSL localhost Caddy → K3s Portal 또는 Compose 서비스**임. 공유기 포트포워딩은 이 경로에 사용하지 않음.

## 현재 ingress 기준

Tunnel의 `~/.cloudflared/config.yml`은 공개 호스트를 WSL의 `https://localhost:443`으로 전달함. Caddy가 host 이름을 기준으로 다음 대상으로 분기함.

| 호스트 | Caddy 대상 |
|---|---|
| `len.pe.kr`, `portal.len.pe.kr`, `file.len.pe.kr`, `admin.len.pe.kr`, `portfolio.len.pe.kr` | K3s `portal-web` NodePort |
| `news.len.pe.kr` | Compose `crawler-worker` |
| `memo.len.pe.kr` | Compose `youtube-memo` |
| `books.len.pe.kr` | Compose `book-memo` |
| `car.len.pe.kr` | Compose `car-care-worker` callback |

Tunnel은 Windows 로그인 뒤 WSL 사용자 서비스로 실행됨. Windows 로그인 유지 작업이 WSL을 살려 두므로 SSH 종료만으로 Tunnel이 내려가면 안 됨.

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

외부 상태는 GitHub Actions가 약 5분 간격으로 별도 점검하며, 장애·복구 전환 시 Telegram 알림을 보냄. 상세는 [공개 상태 Telegram 알림](public-uptime-monitor.md)을 참고함.
