# Nginx Proxy Manager 도메인 라우팅 설정

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | Nginx Proxy Manager 도메인 라우팅 설정 |
| 작성일 | 2026-07-06 |
| 작성자 | Codex |
| 기준 자료 | `docker-compose.yml`, `docker-compose.n100.yml`, `README.md`, 서비스 라우트 코드 |
| 목적 | 미니PC에서 기능별 서브도메인으로 개인 서버 서비스를 분리 운영하기 위함 |
| 비고 | 실제 도메인명은 `example.com` 자리에 교체 필요 |

## 핵심 요약

- `portal-web`은 메인 포털과 `/files`, `/admin/status`를 함께 제공함
- `youtube-memo`, `book-memo`, `crawler-worker`는 별도 서비스로 서브도메인 분리가 가능함
- `system-agent`는 운영용 내부 API 성격이므로 기본적으로 외부 공개를 권장하지 않음
- Nginx Proxy Manager는 같은 Docker Compose 네트워크 안에서 서비스명으로 프록시하면 됨

## 상세 내용

### 1. 권장 도메인 구성

| 서비스 | 권장 서브도메인 | 업스트림 | 경로 비고 |
|---|---|---|---|
| 메인 포털 | `portal.example.com` | `http://portal-web:8000` | 루트(`/`) 연결 |
| 파일함 | `file.example.com` | `http://portal-web:8000` | 루트(`/`) 접속 시 `/files`로 자동 전환함 |
| 관리자 상태 | `admin.example.com` | `http://portal-web:8000` | 루트(`/`) 접속 시 `/admin/status`로 자동 전환함 |
| 뉴스 허브 | `news.example.com` | `http://crawler-worker:8001` | 루트(`/`) 연결 |
| 유튜브 메모 | `memo.example.com` | `http://youtube-memo:8002` | 루트(`/`) 연결 |
| 책 메모 | `books.example.com` | `http://book-memo:8003` | 루트(`/`) 연결 |
| 시스템 상태 | `system.example.com` | `http://system-agent:8010` | 공개 비권장, 내부 확인용 권장 |

### 2. NPM 생성 순서

1. `docker compose -f docker-compose.yml -f docker-compose.n100.yml --profile edge up -d` 실행함
2. 브라우저에서 `http://<미니PC IP>:81`로 Nginx Proxy Manager 접속함
3. `Proxy Hosts`에서 위 서브도메인별 항목을 1개씩 생성함
4. 각 항목의 `Forward Hostname / IP`에 Docker 서비스명 입력함
5. 각 항목의 `Forward Port`에 서비스 포트 입력함
6. `SSL` 탭에서 Let's Encrypt 인증서 발급 후 `Force SSL` 활성화함

### 3. 세부 설정 권장값

| 항목 | 권장값 | 비고 |
|---|---|---|
| Scheme | `http` | 컨테이너 간 통신은 HTTP로 충분함 |
| Forward Hostname | `portal-web`, `crawler-worker`, `youtube-memo`, `book-memo`, `system-agent` | Docker 서비스명 사용함 |
| Forward Port | `8000`, `8001`, `8002`, `8003`, `8010` | 서비스별 포트와 일치시킴 |
| Websockets Support | 필요 시 활성화 | 현재 주요 화면은 필수 아님 |
| Block Common Exploits | 활성화 권장 | 기본 보안 강화 목적 |
| Force SSL | 활성화 권장 | HTTPS 강제용 |
| HTTP/2 Support | 활성화 권장 | 일반적인 HTTPS 성능 향상 목적 |

### 4. `portal-web` 하위 경로 처리

`portal-web`은 하나의 앱에서 메인 화면과 파일함, 관리자 상태 화면을 함께 제공함.

- `https://portal.example.com/`
- `https://file.example.com/` -> 앱에서 자동으로 `/files`로 전환함
- `https://admin.example.com/` -> 앱에서 자동으로 `/admin/status`로 전환함

기능별로 완전히 분리된 느낌을 원하면 NPM에서 `portal.example.com`, `file.example.com`, `admin.example.com`을 모두 `portal-web:8000`으로 연결하고, 앱이 호스트명에 따라 진입 경로를 분기하도록 두는 방식이 적합함.

### 5. 공개 범위 권장

- `portal-web`, `news`, `memo`, `books`는 일반 공개 또는 제한 공개 둘 다 가능함
- `system-agent`는 시스템 메트릭과 운영 상태가 포함되므로 기본적으로 비공개 또는 내부망 제한을 권장함
- NPM 관리자 페이지 `:81`은 외부 공개하지 않는 구성이 안전함

## 검토 결과

- 현재 저장소는 Docker Compose 기반이므로 NPM 도메인 분리와 구조적으로 잘 맞음
- 서비스별 포트와 라우트가 확인되어 서브도메인 분리 기준을 설정할 수 있음
- `portal-web` 내부 경로는 같은 업스트림에 대해 경로 기반 분리가 가능함

## 확인 필요 사항

- 실제 사용할 도메인명 확인 필요
- `system-agent`를 외부에 열지 여부 확인 필요
- `news`, `memo`, `books`를 공개 도메인으로 둘지 내부 접속으로 둘지 확인 필요
- Let's Encrypt 발급을 위한 DNS A 레코드가 미리 설정되어야 함

## 후속 조치

- `.env`의 `NEWS_SERVICE_URL`, `YOUTUBE_MEMO_URL`, `BOOK_MEMO_URL`를 실제 서브도메인으로 변경 필요
- NPM에서 Proxy Host를 생성한 뒤 인증서 발급 필요
- 설정 완료 후 `https://portal.example.com`과 각 서브도메인 접속 확인 필요
