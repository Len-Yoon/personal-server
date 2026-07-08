# Caddy + Cloudflare 운영 가이드

이 문서는 Cloudflare DNS를 유지하면서 Caddy가 공개 HTTPS와 리버스 프록시를 담당하는 운영 구성을 정리합니다.

이 구성은 `len.pe.kr`을 메인 진입점으로 두고, `portal.len.pe.kr`은 호환용 별칭으로 유지하면서 `file.len.pe.kr`, `admin.len.pe.kr`, `news.len.pe.kr`, `memo.len.pe.kr`, `books.len.pe.kr`을 Caddy가 받아 각 서비스로 분기하는 방식입니다.

## 전제

- `len.pe.kr` 도메인이 Cloudflare에 연결되어 있어야 합니다.
- 서버에서 `80`/`443` 포트로 외부 접근이 가능해야 합니다.
- Docker Compose로 앱 컨테이너가 먼저 떠 있어야 합니다.
- Cloudflare API Token에 `Zone:Read`와 `Zone:DNS:Edit` 권한이 있어야 합니다.
- `.env`에 `CADDY_EMAIL`과 `CLOUDFLARE_API_TOKEN`을 넣어야 합니다.

## 포트 구성

- `portal-web`: `127.0.0.1:8000`
- `crawler-worker`: `127.0.0.1:8001`
- `youtube-memo`: `127.0.0.1:8002`
- `book-memo`: `127.0.0.1:8003`
- `caddy`: `80`/`443`

앱은 계속 로컬 바인드로 두고, Caddy 컨테이너가 Docker 내부 네트워크를 통해 각 서비스로 연결합니다.

## 1. 환경 변수 설정

`.env.example`을 기준으로 아래 값을 채웁니다.

```text
CADDY_EMAIL=admin@example.com
CLOUDFLARE_API_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxx
```

## 2. DNS 레코드 정리

Cloudflare에서 공개할 호스트명을 준비합니다.

- `len.pe.kr`
- `portal.len.pe.kr`(호환용 별칭, 선택)
- `file.len.pe.kr`
- `admin.len.pe.kr`
- `news.len.pe.kr`
- `memo.len.pe.kr`
- `books.len.pe.kr`

레코드는 현재 서버 공인 IP를 바라보게 설정합니다.

- Cloudflare 프록시를 켜도 되고, DNS only로 두어도 됩니다.
- Caddy는 DNS-01 challenge로 인증서를 발급하므로 프록시 여부와 무관하게 동작할 수 있습니다.

## 3. 서비스 실행

```bash
docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build
```

`caddy`가 올라오면 자동으로 인증서를 요청하고, 각 서브도메인을 해당 서비스로 프록시합니다.

## 4. 확인

정상 동작 시 아래 주소가 HTTPS로 열립니다.

```text
https://len.pe.kr
https://portal.len.pe.kr
https://file.len.pe.kr
https://admin.len.pe.kr
https://news.len.pe.kr
https://memo.len.pe.kr
https://books.len.pe.kr
```

## 확인 필요 사항

- 공유기 또는 방화벽에서 `80`/`443` 인바운드가 막혀 있으면 이 구성은 동작하지 않습니다.
- 외부 포트포워딩이 어려우면 `docs/cloudflare-tunnel.md`의 Tunnel 구성을 사용해야 합니다.
- `news.len.pe.kr`은 `crawler-worker`가 실행 중이어야 응답합니다.
