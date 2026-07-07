# Cloudflare Tunnel 운영 가이드

이 저장소는 집 안/밖 모두에서 `len.pe.kr` 계열 서비스를 열 수 있지만, 현재 회선처럼 공유기 포트포워딩이 막히는 환경에서는 Cloudflare Tunnel이 가장 단순합니다.

이 문서는 `portal.len.pe.kr`, `file.len.pe.kr`, `admin.len.pe.kr`, `news.len.pe.kr`, `memo.len.pe.kr`, `books.len.pe.kr`을 Cloudflare Tunnel로 연결하는 기준 운영 절차를 정리합니다.

## 전제

- `len.pe.kr` 도메인의 네임서버가 Cloudflare로 이전되어 있어야 합니다.
- `cloudflared tunnel login`이 WSL/Linux 환경에서 성공해 `~/.cloudflared/cert.pem`이 생성되어 있어야 합니다.
- Docker Compose로 앱 컨테이너가 먼저 떠 있어야 합니다.
- Tunnel은 로컬 포트로 직접 붙으므로, 외부 공개용 `80`/`443` 포트포워딩은 필요하지 않습니다.

## 서비스 포트

이 저장소의 기본 포트 매핑은 다음과 같습니다.

- `portal-web`: `127.0.0.1:8000`
- `crawler-worker`: `127.0.0.1:8001`
- `youtube-memo`: `127.0.0.1:8002`
- `book-memo`: `127.0.0.1:8003`

`portal-web`은 `/`, `/files`, `/admin/status`를 함께 처리하므로 `portal`, `file`, `admin` 호스트를 모두 `8000`으로 보냅니다.

## 1. tunnel 생성

WSL/Linux 셸에서 실행합니다.

```bash
cloudflared tunnel create personal-server
```

출력되는 `Tunnel UUID`와 credentials 파일 경로를 기록해 둡니다.

## 2. 설정 파일 작성

`~/.cloudflared/config.yml` 예시는 아래와 같습니다.

```yaml
tunnel: <Tunnel-UUID>
credentials-file: /home/<your-linux-user>/.cloudflared/<Tunnel-UUID>.json

ingress:
  - hostname: portal.len.pe.kr
    service: http://localhost:8000
  - hostname: file.len.pe.kr
    service: http://localhost:8000
  - hostname: admin.len.pe.kr
    service: http://localhost:8000
  - hostname: news.len.pe.kr
    service: http://localhost:8001
  - hostname: memo.len.pe.kr
    service: http://localhost:8002
  - hostname: books.len.pe.kr
    service: http://localhost:8003
  - service: http_status:404
```

## 3. DNS 연결

터널 이름을 각 호스트에 연결합니다.

```bash
cloudflared tunnel route dns personal-server portal.len.pe.kr
cloudflared tunnel route dns personal-server file.len.pe.kr
cloudflared tunnel route dns personal-server admin.len.pe.kr
cloudflared tunnel route dns personal-server news.len.pe.kr
cloudflared tunnel route dns personal-server memo.len.pe.kr
cloudflared tunnel route dns personal-server books.len.pe.kr
```

Cloudflare 대시보드에서 DNS 레코드는 `CNAME` 기반으로 정리됩니다. 같은 이름에 남아 있는 `A` 레코드는 제거해야 합니다.

## 4. tunnel 실행

```bash
cloudflared tunnel run personal-server
```

정상 동작 시 아래 주소가 HTTPS로 열립니다.

```text
https://portal.len.pe.kr
https://file.len.pe.kr
https://admin.len.pe.kr
https://news.len.pe.kr
https://memo.len.pe.kr
https://books.len.pe.kr
```

## 운영 팁

- 외부 공개는 Tunnel이 담당하므로 공유기 `80`/`443` 포트포워딩은 끄는 편이 깔끔합니다.
- 앱 컨테이너는 계속 `127.0.0.1` 바인드로 두어도 됩니다.
- `cloudflared tunnel login` 후 `~/.cloudflared`가 비어 있으면, 같은 WSL 세션에서 다시 로그인해야 할 수 있습니다.
- `len.pe.kr` 네임서버가 아직 Cloudflare에 위임되지 않았다면 Tunnel 생성이나 DNS 연결이 제대로 진행되지 않습니다.
