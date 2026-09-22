# Cloudflare Tunnel 운영 가이드

현재 공개 경로는 **Cloudflare Tunnel → WSL loopback Caddy → K3s Portal·Crawler Worker·Book Memo·YouTube Memo**로 제공함. Hyundai OAuth callback인 `car.len.pe.kr`은 Caddy 경로에 포함하지 않음. 공유기 포트포워딩은 이 경로에 사용하지 않음.

## 현재 ingress 기준

2026-09-17 N100에서 `cloudflared-personal-server.service` active, 뉴스·Portal 외부 health 3회 HTTP 200, Caddy 내부 뉴스 health를 대조함. Tunnel ID, 인증 파일, private IP는 문서에 기록하지 않음.

| 호스트 | Tunnel 대상 유형 | 최종 대상 |
|---|---|---|
| `len.pe.kr`, `portal.len.pe.kr`, `file.len.pe.kr`, `admin.len.pe.kr`, `portfolio.len.pe.kr` | WSL loopback Caddy | K3s `portal-web` NodePort |
| `news.len.pe.kr` | WSL loopback Caddy | K3s `crawler-worker` Service |
| `memo.len.pe.kr` | WSL loopback Caddy | K3s `youtube-memo` Service |
| `books.len.pe.kr` | WSL loopback Caddy | K3s `book-memo` Service |
| `car.len.pe.kr` | `car-care-worker` 비공개 callback upstream | Hyundai OAuth callback |

뉴스는 Caddy를 거쳐 K3s `crawler-worker` Service로 연결되며, Book Memo·YouTube Memo도 각각 K3s Service로 연결됨. Caddy 경유 여부나 private callback upstream은 Tunnel 설정 대조 결과이며, 주소 값 자체는 운영 문서에 기록하지 않음.

Crawler Worker는 Docker 중지, PVC 데이터 검증, K3s readiness, Caddy 내부 health, Tunnel 전환과 외부 health 3회 확인을 마쳐 현재 K3s 단일 writer로 운영함. 상세 경계는 [운영 참조](operations-reference.md#뉴스-수집-k3s-현재-운영-기준)를 따름.

## 차량 OAuth callback ingress

`Caddyfile`에는 `car.len.pe.kr` 호스트 블록이 없음. 따라서 Hyundai OAuth callback은 Caddy 대상 표에 추가하지 않으며, Cloudflare Tunnel의 별도 ingress가 `car-care-worker` 비공개 callback upstream으로 전달함. private callback upstream의 주소는 저장소·문서·로그에 기록하지 않음.

이 Tunnel 설정은 저장소 밖의 `~/.cloudflared/config.yml`에 있으므로 실제 운영 값은 변경 전 읽기 전용 대조가 필요함. 차량 callback ingress가 없거나 Caddy로 잘못 전달되면 `HYUNDAI_REDIRECT_URI`의 callback이 404가 될 수 있음.

Tunnel은 Windows 로그인 뒤 WSL 사용자 서비스로 실행됨. `PersonalServer-WSL-KeepAlive`가 WSL을 유지하고, `personal-server-autostart`의 Supervisor가 Daemon을 단일 관리함. Supervisor는 초기 120초 대기 뒤 Daemon을 시작하며, Daemon은 3분 간격으로 WSL·K3s·Portal·NodePort·Tunnel을 점검함. Daemon이 비정상 종료되면 Supervisor가 15초 뒤 재기동하고, 짧은 시간에 3회 연속 종료되면 60초 backoff를 적용함. 같은 구성요소가 2회 연속 비정상이면 승인된 제한 복구를 시도하며, 구성요소별 시도는 최대 3회임. 복구 상태를 기록하지 못하면 중복·무한 복구를 막기 위해 추가 복구를 중단함. SSH 종료만으로 Tunnel이 내려가면 안 됨. Tunnel 단독 장애에는 호스트 긴급 재부팅을 사용하지 않음.

## 전송 프로토콜 기준

2026-09-22 N100에서 QUIC용 UDP 통신이 차단되고 HTTP/2용 TCP 연결은 정상인 상태를 확인함. QUIC 연결 손실로 Tunnel 프로세스가 종료되어 3분 감시 주기마다 장애·복구 알림이 반복됐음. `cloudflared-personal-server.service`의 systemd drop-in에서 실행 명령을 `cloudflared tunnel --protocol http2 run personal-server`로 재정의해 HTTP/2를 고정함.

적용 뒤 Tunnel 연결 4개가 HTTP/2로 등록되고, Portal·News 공개 health와 Caddy 내부 NodePort health가 모두 HTTP 200임을 확인함. QUIC 차단 자체는 해소되지 않았으나, HTTP/2가 정상 통신 경로로 사용되므로 방화벽·공유기·WSL 네트워크 설정을 추가 변경하지 않음. ICMP proxy 경고는 ping 기능 제한에 관한 것으로 Tunnel의 HTTP 요청 연결 장애 원인으로 처리하지 않음.

원래 전송 방식으로 되돌려야 하는 경우에는 HTTP/2 drop-in만 제거하고 systemd daemon-reload 뒤 같은 사용자 서비스를 한 번 재시작함. Caddy·Tunnel ingress·K3s·PVC·Secret·다른 서비스는 rollback 대상이 아님.

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
| Cloudflare 502 | Tunnel은 연결됐지만 내부 대상 응답 실패 | Caddy, K3s `portal-web`·`crawler-worker`·`book-memo`·`youtube-memo` Service 상태 확인 |

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
