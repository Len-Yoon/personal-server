# 운영 문서 정합성 점검 설계

## 목적

현재 코드·Compose·환경변수 예시·GitHub Actions를 기준으로 운영 문서의 오래된 설명을 정리하고, 문서의 현재성 여부를 독자가 즉시 판단할 수 있도록 구조화함.

## 범위

- `README.md`의 문서 링크를 운영 문서 색인으로 연결함.
- `docs/agent-handoff.md`, `docs/caddy-cloudflare.md`, `docs/cloudflare-tunnel.md`, `docs/n100-mt4-setup.md`, `docs/n100-github-auto-deploy.md`를 현재 구성과 대조해 갱신함.
- 새 `docs/operations-reference.md`에 환경변수 범주, 공개 경로, 확인 명령을 요약함.
- 과거 시점의 QA 보고서와 설계·계획 문서는 기록으로 보존하고, 최신 운영 기준이 아니라는 점을 표시함.

## 근거 자료

- `docker-compose.yml`, `docker-compose.n100.yml`
- `.env.example`, `caddy/Caddyfile`
- `scripts/windows-bootstrap.ps1`, `scripts/windows-bootstrap.sh`, `scripts/deploy-n100.sh`
- `.github/workflows/ci.yml`, `.github/workflows/deploy-n100.yml`
- 뉴스·인증·파일함 관련 현재 테스트와 서비스 코드

## 제약 사항

- 서버 기동, 스케줄러, Compose 구성은 수정하지 않음.
- 비밀값과 개인 계정 정보는 문서에 기록하지 않음.
- 과거 계획·QA의 당시 결론을 현재 사실처럼 수정하지 않고, 상태 표기만 추가함.

## 검증 기준

- README와 운영 문서 사이의 Markdown 링크 대상이 존재해야 함.
- 문서의 서비스명·포트·도메인·배포 workflow 명칭이 현재 구성 파일과 일치해야 함.
- `git diff --check`가 통과해야 함.
