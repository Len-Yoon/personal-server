# 작업 인수인계

이 문서는 현재 저장소를 이어서 개발·운영할 작업자를 위한 빠른 참조임. 서비스·포트·도메인·운영 명령의 최신 기준은 [운영 참조](operations-reference.md)를 우선함.

## 1. 서비스 구성

| 서비스 | 책임 | 주요 코드 |
|---|---|---|
| `portal-web` | 포털, 파일함, 관리자 상태, 포트폴리오 | `portal-web/app/routers/`, `portal-web/app/services/` |
| `system-agent` | Docker·백업·host metrics 상태 API | `system-agent/app/services/metrics.py` |
| `crawler-worker` | Investing.com 한국어 RSS 보관, 나스닥 관련성 분류, Telegram 중요 알림 | `crawler-worker/app/services/news_archive.py`, `nasdaq_relevance.py` |
| `youtube-memo` | YouTube 영상·타임스탬프 메모 | `youtube-memo/app/main.py` |
| `book-memo` | 책·목차·독서 메모 | `book-memo/app/main.py` |
| `caddy` | 외부 `80`·`443`을 여는 경우의 대체 HTTPS 프록시 | `caddy/Caddyfile` |

## 2. 운영 구조

- N100 override는 앱 포트를 `127.0.0.1`에만 바인드함.
- Windows bootstrap은 host metrics를 기록하고, Docker 스택과 Cloudflare Tunnel 프로세스를 확인함.
- `main` push는 GitHub Actions `Deploy N100` workflow를 통해 Windows self-hosted runner에서 배포됨.
- `scripts/deploy-n100.sh`는 원격 `origin/main`으로 코드 추적 파일을 맞추므로 N100 작업 디렉터리에서 추적 파일을 직접 수정하지 않음.

## 3. 인증 경계

- `ADMIN_STATUS_PASSWORD`: 관리자 상태의 우선 비밀번호임. 없으면 이전 환경변수 순서로 대체함.
- `FILE_MANAGER_ACCESS_PASSWORD`: 파일함 진입 세션용 비밀번호임.
- `DELETE_PASSWORD`: 파일함 삭제와 책·YouTube 메모 쓰기 로그인에 사용함.
- `PORTFOLIO_ADMIN_PASSWORD`: 포트폴리오 편집 전용 비밀번호임.
- 책·YouTube 메모 삭제는 유효한 쓰기 로그인 세션과 삭제 확인을 요구하며, 삭제 시 비밀번호를 재입력하지 않음.

## 4. 변경 시 확인할 계약

| 변경 영역 | 반드시 유지할 계약 |
|---|---|
| 파일함 | 접근 인증, 업로드 확장자·용량 제한, 경로 안전성, 업로드·다운로드·삭제 흐름 |
| 관리자 상태 | Windows host가 기록한 `captured_at`과 화면 조회 시각을 혼동하지 않음 |
| 뉴스 | 전망성 기사는 archive, 확정된 시장 충격만 Telegram alert 정책 유지 |
| 메모 | 공개 읽기와 세션 기반 쓰기 분리, unsafe 요청 Origin 검증 유지 |
| 공개 서비스 | CSP·보안 헤더·정적 파일 응답 정책 유지 |

## 5. 검증과 배포

- 단위 테스트는 `.github/workflows/ci.yml`의 서비스별 명령을 기준으로 실행함.
- 변경 범위가 넓으면 포털·system-agent·crawler-worker·YouTube·책 테스트를 모두 실행하고 `git diff --check`를 확인함.
- 배포 실패나 N100 상태 확인은 [N100 GitHub 자동배포 안내](n100-github-auto-deploy.md)의 WSL 명령을 사용함.
- 서버 기동과 스케줄러 영역은 기능 변경 작업에서 수정하지 않음.

## 6. 관련 문서

- [운영 문서 색인](README.md)
- [N100 운영 환경](n100-mt4-setup.md)
- [Cloudflare Tunnel](cloudflare-tunnel.md)
- [Caddy + Cloudflare](caddy-cloudflare.md)
- [자동 배포](n100-github-auto-deploy.md)
