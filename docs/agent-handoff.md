# Agent Handoff

새 채팅에서 이 프로젝트를 빠르게 파악하기 위한 요약입니다.

## Project

`personal-server`는 Docker Compose로 묶은 개인 서버 포트폴리오 프로젝트입니다.

- `portal-web`: 메인 포털, 파일함, 보안 상태 모달
- `system-agent`: 미니 PC/Windows host metrics, 백업/파일함/컨테이너 상태 API
- `crawler-worker`: Google News RSS 수집, 저장 뉴스, OpenAI 요약
- `youtube-memo`: YouTube 링크별 메모
- `book-memo`: 책 검색, 독서 진행률, 목차/메모 관리
- `nginx-proxy-manager`: 외부 도메인/SSL 프록시

## Important Files

- `README.md`: 포트폴리오용 설명과 서비스별 스크린샷
- `.env.example`: 빈 값 예시만 유지
- `docker-compose.yml`: 로컬/운영 compose
- `docker-compose.n100.yml`: N100/MT4 동시 운영용 경량 compose override
- `docs/images/`: README 스크린샷
- `docs/n100-mt4-setup.md`: Windows N100 + MT4 + WSL2 운영 지침
- `portal-web/app/services/security.py`: 보안 헤더, 일별 보안 로그, 보안 상태 데이터
- `portal-web/app/services/file_store.py`: 파일함 저장소, 업로드 정책, 경로 안전 처리
- `portal-web/app/routers/dashboard.py`: 포털 대시보드, `/admin/security`
- `portal-web/app/routers/files.py`: 파일함 라우터와 인증/삭제 보호
- `scripts/maintenance.py`: SQLite 백업, 파일함 선택 백업, 보안 로그 정리
- `scripts/windows-host-metrics.ps1`: Windows N100 host 상태를 `data/system/host-metrics.json`으로 기록
- `system-agent/app/services/metrics.py`: system-agent metrics 수집/정규화
- `portal-web/app/services/system_status.py`: 포털 dashboard용 agent fetch, demo/fallback 처리
- `portal-web/app/services/global_search.py`: 서비스별 검색 API 집계
- `tests/test_portal_security.py`: 파일함/보안 로그 핵심 unittest

## Current Security Work

- `portal-web`에 보안 헤더 미들웨어 추가.
- 파일 업로드 제한 추가:
  - 최대 크기: `FILE_MAX_UPLOAD_MB`
  - 차단 확장자: `FILE_BLOCKED_EXTENSIONS`
  - 허용 확장자 allowlist: `FILE_ALLOWED_EXTENSIONS`
  - 확장자 없는 파일 차단
  - 같은 이름 덮어쓰기 차단
- 보안 이벤트 로그 추가:
  - 파일함 인증 실패
  - 업로드 성공/차단
  - 다운로드
  - 삭제 성공/실패
  - 보안 대시보드 조회/실패
- 로그는 일별 파일로 생성:
  - 기준 env: `SECURITY_LOG_PATH`
  - 예: `security-events-2026-06-29.txt`
  - 시간대 env: `SECURITY_LOG_TIMEZONE`
- 포털 `보안 상태` 버튼 추가.
- 보안 상태 모달은 기존 관리자 패스워드 필요:
  - 우선 `FILE_MANAGER_PASSWORD`
  - 없으면 `DELETE_PASSWORD`
- 포털 서비스 링크는 env 기반:
  - `NEWS_SERVICE_URL`
  - `YOUTUBE_MEMO_URL`
  - `BOOK_MEMO_URL`
- 운영 모드 파일함 보호:
  - `APP_ENV=production` 또는 `FILE_MANAGER_AUTH_REQUIRED=true`이면 `FILE_MANAGER_PASSWORD` 없을 때 `/files` 접근 차단

## Maintenance Work

- `scripts/maintenance.py backup`
  - `data/*/*.sqlite3`를 `BACKUP_PATH` 아래 타임스탬프 폴더로 백업
  - `BACKUP_INCLUDE_FILES=true`일 때 `data/files/`를 zip 백업
- `scripts/maintenance.py prune-logs`
  - `SECURITY_LOG_RETENTION_DAYS`보다 오래된 일별 보안 로그 삭제
- `scripts/maintenance.py all`
  - 백업과 로그 정리를 함께 실행

## Mini PC Dashboard Work

- `system-agent` 서비스 추가:
  - `/health`
  - `/metrics`
  - `/metrics/demo`
- 포털 첫 화면에 미니 PC 상태 섹션 추가:
  - CPU, 메모리, 디스크, 파일함 개수, 최근 백업
  - 컨테이너 상태 목록
  - `host_metrics_missing`, `host_metrics_stale`, `backup_missing`, `system_agent_unavailable` 같은 경고
- Windows 전체 host 상태는 Docker 컨테이너가 직접 보지 않고 PowerShell collector가 JSON으로 넘김.
- `DEMO_MODE=true`이면 샘플 서버 상태와 샘플 검색 결과를 표시.
- 포털 전체 검색 추가:
  - `crawler-worker /api/search`
  - `youtube-memo /api/search`
  - `book-memo /api/search`

## README / Screenshots

README에 서비스별 스크린샷과 설명을 추가했습니다.

- `docs/images/portal-dashboard.png`
- `docs/images/file-manager.png`
- `docs/images/news-hub.png`
- `docs/images/youtube-memo.png`
- `docs/images/book-memo.png`

파일함 스크린샷은 `예시 폴더1`이 보이는 상태로 다시 캡처했습니다.

## Env / Git Safety

- 실제 `.env`는 커밋 금지.
- `.env.example`은 빈 값 예시만 둠.
- `data/`, SQLite DB, 로그, Nginx Proxy Manager 데이터는 커밋 금지.
- `.gitignore`가 `.env`, `data/`, `*.sqlite3`, `*.db`, 로그, IDE 파일을 무시함.

## Open Items

추천 우선순위:

1. Windows 작업 스케줄러에 `scripts/windows-host-metrics.ps1` 연결.
2. 백업/로그 정리를 N100 cron 또는 Windows 작업 스케줄러에 연결.
3. GitHub 공개 전 secret scan.
4. Telegram 등으로 백업 실패/디스크 부족 알림 추가.
5. Docker socket 기반 실제 컨테이너 상태 수집은 필요할 때만 신중히 추가.

## Verification Already Done

- `python3 -m compileall portal-web/app` 통과.
- `PYTHONPATH=system-agent python3 -m unittest tests.system_agent.test_metrics`로 system-agent metrics 테스트 가능.
- Docker 서비스가 떠 있는 상태에서 Playwright + 로컬 Chrome로 README 스크린샷 캡처 완료.
- 일별 로그 파일명 생성 확인.
- 파일함 업로드 차단 확장자 검사 확인.
- `PYTHONPATH=portal-web python3 -m unittest discover -s tests`로 핵심 테스트 실행 가능.
