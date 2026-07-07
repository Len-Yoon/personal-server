# Caddy 도메인 라우팅 설정

> This document describes the direct Caddy ingress path. For the current default external-access setup, prefer [Cloudflare Tunnel](docs/cloudflare-tunnel.md).

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | Caddy 도메인 라우팅 설정 |
| 작성일 | 2026-07-07 |
| 작성자 | Codex |
| 기준 자료 | `docker-compose.yml`, `docker-compose.n100.yml`, `README.md`, 서비스 라우트 코드 |
| 목적 | 미니PC에서 기능별 서브도메인으로 개인 서버 서비스를 분리 운영하기 위함 |
| 비고 | 실제 도메인명은 `len.pe.kr` 기준으로 작성함 |

## 핵심 요약

- Caddy는 도메인별로 reverse proxy를 두고 자동 HTTPS를 처리함
- `portal-web`은 메인 포털과 `/files`, `/admin/status`를 함께 제공함
- `youtube-memo`, `book-memo`, `crawler-worker`는 별도 서비스로 서브도메인 분리가 가능함
- `system-agent`는 운영용 내부 API 성격이므로 기본적으로 외부 공개를 권장하지 않음

## 상세 내용

### 1. 권장 도메인 구성

| 서비스 | 권장 서브도메인 | 업스트림 | 경로 비고 |
|---|---|---|---|
| 메인 포털 | `portal.len.pe.kr` | `http://portal-web:8000` | 루트(`/`) 연결 |
| 파일함 | `file.len.pe.kr` | `http://portal-web:8000` | 루트(`/`) 접속 시 `/files`로 자동 전환함 |
| 관리자 상태 | `admin.len.pe.kr` | `http://portal-web:8000` | 루트(`/`) 접속 시 `/admin/status`로 자동 전환함 |
| 뉴스 허브 | `news.len.pe.kr` | `http://crawler-worker:8001` | 루트(`/`) 연결 |
| 유튜브 메모 | `memo.len.pe.kr` | `http://youtube-memo:8002` | 루트(`/`) 연결 |
| 책 메모 | `books.len.pe.kr` | `http://book-memo:8003` | 루트(`/`) 연결 |
| 시스템 상태 | `system.len.pe.kr` | `http://system-agent:8010` | 공개 비권장, 내부 확인용 권장 |

### 2. Caddyfile 예시

저장소 루트의 `Caddyfile`에서 도메인별 reverse proxy를 정의함.

```caddyfile
portal.len.pe.kr {
	reverse_proxy portal-web:8000
}

file.len.pe.kr {
	reverse_proxy portal-web:8000
}

admin.len.pe.kr {
	reverse_proxy portal-web:8000
}

news.len.pe.kr {
	reverse_proxy crawler-worker:8001
}

memo.len.pe.kr {
	reverse_proxy youtube-memo:8002
}

books.len.pe.kr {
	reverse_proxy book-memo:8003
}
```

### 3. 실행 순서

1. `docker compose -f docker-compose.yml -f docker-compose.n100.yml --profile edge up -d --build` 실행함
2. `Caddyfile`의 도메인과 실제 DNS A 레코드를 일치시킴
3. `80`, `443` 포트가 Caddy로 전달되도록 공유기 또는 방화벽 설정을 확인함
4. Caddy가 Let's Encrypt 인증서를 자동 발급하고 갱신하는지 확인함

### 4. 세부 설정 권장값

| 항목 | 권장값 | 비고 |
|---|---|---|
| reverse_proxy 대상 | `portal-web`, `crawler-worker`, `youtube-memo`, `book-memo`, `system-agent` | Docker 서비스명 사용함 |
| 포트 | `8000`, `8001`, `8002`, `8003`, `8010` | 서비스별 포트와 일치시킴 |
| 공개 범위 | `portal`, `news`, `memo`, `books`는 공개 가능 | 사용 목적에 따라 제한 가능 |
| 내부 전용 | `system-agent` | 기본적으로 외부 공개 비권장 |
| 인증서 | Caddy 자동 HTTPS | 별도 Certbot 설정이 필요 없음 |

### 5. `portal-web` 하위 경로 처리

`portal-web`은 하나의 앱에서 메인 화면과 파일함, 관리자 상태 화면을 함께 제공함.

- `https://portal.len.pe.kr/`
- `https://file.len.pe.kr/` -> 앱에서 자동으로 `/files`로 전환함
- `https://admin.len.pe.kr/` -> 앱에서 자동으로 `/admin/status`로 전환함

기능별로 완전히 분리된 느낌을 원하면 `portal.len.pe.kr`, `file.len.pe.kr`, `admin.len.pe.kr`을 모두 `portal-web:8000`으로 연결하고, 앱이 호스트명에 따라 진입 경로를 분기하도록 두는 방식이 적합함.

### 6. 공개 범위 권장

- `portal-web`, `news`, `memo`, `books`는 일반 공개 또는 제한 공개 둘 다 가능함
- `system-agent`는 시스템 메트릭과 운영 상태가 포함되므로 기본적으로 비공개 또는 내부망 제한을 권장함
- Caddy의 `Caddyfile`은 저장소에 두되, 실제 DNS와 포트포워딩은 운영 환경에서 별도로 맞춰야 함

## 검토 결과

- 현재 저장소는 Docker Compose 기반이므로 Caddy 도메인 분리와 구조적으로 잘 맞음
- 서비스별 포트와 라우트가 확인되어 서브도메인 분리 기준을 설정할 수 있음
- `portal-web` 내부 경로는 같은 업스트림에 대해 경로 기반 분리가 가능함

## 확인 필요 사항

- 실제 사용할 도메인명 확인 필요
- `system-agent`를 외부에 열지 여부 확인 필요
- `news`, `memo`, `books`를 공개 도메인으로 둘지 내부 접속으로 둘지 확인 필요
- Let's Encrypt 발급을 위한 DNS A 레코드가 미리 설정되어야 함

## 후속 조치

- `.env`의 `NEWS_SERVICE_URL`, `YOUTUBE_MEMO_URL`, `BOOK_MEMO_URL`를 실제 서브도메인으로 변경 필요
- `Caddyfile`에서 실제 공개 도메인과 업스트림을 다시 확인 필요
- 설정 완료 후 `https://portal.len.pe.kr`과 각 서브도메인 접속 확인 필요
