# Agent Handoff

이 문서는 다음 작업자가 이 저장소를 빠르게 이해하고 이어서 작업할 수 있도록 정리한 인수인계 문서임.

## 1. 프로젝트 개요

`personal-server`는 Docker Compose로 묶은 개인용 서비스 모음임.

- `portal-web`: 메인 포털, 파일함, 관리자 상태 화면
- `system-agent`: N100/Windows host metrics, 백업/파일함/컨테이너 상태 API
- `crawler-worker`: Google News RSS 수집, AI 요약, 저장 뉴스 관리
- `youtube-memo`: YouTube 링크별 메모
- `book-memo`: 책 검색, 독서 진행률, 목차별 코멘트, 독서 메모

## 2. 핵심 문서

- [`README.md`](../README.md): 프로젝트 개요, 실행 방법, 운영 기준
- [`.env.example`](../.env.example): 환경 변수 예시
- [`docs/caddy-cloudflare.md`](caddy-cloudflare.md): Caddy + Cloudflare 공개 HTTPS 운영
- [`docs/cloudflare-tunnel.md`](cloudflare-tunnel.md): Cloudflare Tunnel 운영
- [`docs/n100-mt4-setup.md`](n100-mt4-setup.md): Windows N100 + MT4 + WSL2 운영
- [`docs/n100-github-auto-deploy.md`](n100-github-auto-deploy.md): GitHub Actions 자동배포와 Runner 운영
- [`docs/20260702_운영보안QA_점검보고서.md`](20260702_운영보안QA_점검보고서.md): 운영보안 QA 점검 보고서

## 3. 핵심 코드 위치

- [`portal-web/app/services/security.py`](../portal-web/app/services/security.py): 보안 헤더, 인증 실패 기록, 보안 상태
- [`portal-web/app/services/file_store.py`](../portal-web/app/services/file_store.py): 파일 저장, 업로드 제한, 경로 안전 처리
- [`portal-web/app/routers/dashboard.py`](../portal-web/app/routers/dashboard.py): 포털 메인, `/admin/status`
- [`portal-web/app/routers/files.py`](../portal-web/app/routers/files.py): 파일함 라우트와 다운로드/삭제 보호
- [`portal-web/app/services/global_search.py`](../portal-web/app/services/global_search.py): 서비스 통합 검색
- [`system-agent/app/services/metrics.py`](../system-agent/app/services/metrics.py): host metrics 집계
- [`scripts/maintenance.py`](../scripts/maintenance.py): SQLite 백업, 로그 정리

## 4. 현재 운영 기준

### 4.1 메인 도메인

- 메인 진입점은 `https://len.pe.kr` 기준으로 정리함
- `portal.len.pe.kr`은 호환용 별칭으로 유지 가능함

### 4.2 공개 HTTPS

- 공개 HTTPS는 Caddy + Cloudflare DNS challenge 방식 사용
- 외부 포트포워딩이 어렵다면 Cloudflare Tunnel 사용

### 4.3 N100 운영

- N100 override에서는 앱 포트를 `127.0.0.1`에만 바인드함
- `caddy`는 `80`/`443`을 받아 리버스 프록시와 인증서를 담당함
- `crawler-worker`는 기본 운영 스택에 포함함
- `investing-crawler`는 Windows 작업 스케줄러가 필요할 때만 실행하는 일회성 컨테이너임
- `main` push는 상시 서비스만 재배포하며 `investing-crawler`는 실행하지 않음

## 5. 보안 및 운영 포인트

- `.env`에는 비밀값만 저장하고 문서에는 남기지 않음
- `FILE_MANAGER_ACCESS_PASSWORD`는 파일함 진입, `DELETE_PASSWORD`는 파일 삭제에 사용함
- `APP_ENV=production` 또는 `FILE_MANAGER_AUTH_REQUIRED=true`로 파일함 인증을 강제해야 함
- `BACKUP_INCLUDE_FILES=true`는 파일함 백업이 필요할 때만 사용함
- `system-agent`는 기본적으로 비공개 운영을 권장함

## 6. 배포 및 장애 대응

- GitHub Actions의 `Deploy N100`이 성공하면 `portal-web`, `system-agent`, `crawler-worker`, `youtube-memo`, `book-memo`, `caddy`만 재빌드·재기동함.
- N100 배포는 Windows Runner 서비스가 `.\window` 계정으로 실행되고, WSL2의 `Ubuntu-24.04`에서 Docker를 사용할 수 있어야 함.
- 수동 배포가 필요하면 `docs/n100-github-auto-deploy.md`의 WSL 명령을 사용함. Windows PowerShell에서 `docker`를 직접 실행하는 방식은 현재 운영 경로가 아님.
- `investing-crawler`는 자동배포로 시작하지 않으며 Windows 작업 스케줄러 또는 `scripts/investing-news-once.ps1`에서 별도로 실행함.

## 7. 확인 필요 사항

- N100 PC에서 `80`/`443` 인바운드가 실제로 열려 있는지 확인 필요함
- Cloudflare DNS의 `len.pe.kr` A 레코드가 N100 공인 IP를 가리키는지 확인 필요함
- `.env`의 `CADDY_EMAIL`, `CLOUDFLARE_API_TOKEN` 값이 정상인지 확인 필요함
- `portal.len.pe.kr` 별칭을 계속 유지할지 최종 결정 필요함

## 8. 후속 조치

- 자동배포가 중단된 경우에만 N100에서 `wsl.exe -d Ubuntu-24.04 -- bash -lc "cd /mnt/c/personal-server && bash ./scripts/deploy-n100.sh"`를 수동 실행함
- Caddy 로그를 확인해 인증서 발급 여부를 점검해야 함
- 외부 포트포워딩이 불가하면 Cloudflare Tunnel 구성으로 전환 필요함
- GitHub push 기반 자동배포의 Runner 설치·서비스 계정·장애 대응은 [`docs/n100-github-auto-deploy.md`](n100-github-auto-deploy.md)를 참고함
