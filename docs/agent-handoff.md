# Agent Handoff

새 채팅에서 이 프로젝트를 빠르게 파악하기 위한 요약입니다.

## Project

`personal-server`는 Docker Compose로 묶은 개인 서버 포트폴리오 프로젝트입니다.

- `portal-web`: 메인 포털, 파일함, 보안 상태 모달
- `crawler-worker`: Google News RSS 수집, 저장 뉴스, OpenAI 요약
- `youtube-memo`: YouTube 링크별 메모
- `book-memo`: 책 검색, 독서 진행률, 목차/메모 관리
- `nginx-proxy-manager`: 외부 도메인/SSL 프록시

## Important Files

- `README.md`: 포트폴리오용 설명과 서비스별 스크린샷
- `.env.example`: 빈 값 예시만 유지
- `docker-compose.yml`: 로컬/운영 compose
- `docs/images/`: README 스크린샷
- `docs/portfolio-release-guidelines.md`: 공개 전 체크리스트
- `portal-web/app/services/security.py`: 보안 헤더, 일별 보안 로그, 보안 상태 데이터
- `portal-web/app/services/file_store.py`: 파일함 저장소, 업로드 정책, 경로 안전 처리
- `portal-web/app/routers/dashboard.py`: 포털 대시보드, `/admin/security`
- `portal-web/app/routers/files.py`: 파일함 라우터와 인증/삭제 보호

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

1. 실제 도메인 하드코딩 env화 또는 예시 도메인으로 변경.
2. 운영 모드에서 `FILE_MANAGER_PASSWORD` 없으면 `/files` 차단.
3. 포트폴리오용 `DEMO_MODE` 추가.
4. 파일 경로 탈출, 확장자 차단, 일별 로그, 관리자 인증 테스트 추가.
5. GitHub 공개 전 secret scan.

## Verification Already Done

- `python3 -m compileall portal-web/app` 통과.
- Docker 서비스가 떠 있는 상태에서 Playwright + 로컬 Chrome로 README 스크린샷 캡처 완료.
- 일별 로그 파일명 생성 확인.
- 파일함 업로드 차단 확장자 검사 확인.
