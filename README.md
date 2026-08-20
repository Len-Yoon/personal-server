# Personal Server

> 개인 생산성과 홈 서버 운영에 필요한 기능을 하나의 웹 플랫폼으로 통합한 프로젝트입니다.

Windows N100과 WSL2 환경에서 Docker Compose로 운영하며, 포털·파일함·서버 상태·뉴스·메모·포트폴리오를 서비스별로 분리했습니다.

## Features

| 영역 | 주요 기능 |
|---|---|
| Portal | 서비스 허브, 뉴스·YouTube·책 메모 통합 검색, 관리자 상태 진입 |
| File Manager | 다중 업로드, 드래그 앤 드롭, 폴더 생성, 검색·정렬, 아이콘/목록 보기, ZIP 일괄 다운로드 |
| System Status | Windows host의 실제 수집 시각을 기준으로 CPU·메모리·디스크·백업·컨테이너 상태 표시 |
| HomeOps | Compose 서비스 진단, 정책 기반 재시작, health 검증, SQLite 이력, Telegram 상태 알림 |
| News Hub | Investing.com·Google News RSS 수집, 나스닥 관련성 분류, 중요 기사 Telegram 알림 |
| Memos | YouTube 영상·타임스탬프 메모, 책·목차·독서 메모 |
| Portfolio | Markdown 기반 공개 포트폴리오와 인증된 편집 화면 |

## Highlights

- 시장 충격 기사와 전망성 기사를 분리해 중요한 뉴스만 알림 처리
- 화면 조회 시각이 아닌 Windows host의 실제 수집 시각으로 상태 판단 기준 통일
- 읽기 공개와 쓰기 권한을 분리하고 세션 인증·Origin 검증·인증 실패 제한 적용
- `main` CI 성공 뒤 Windows self-hosted runner가 N100 배포와 health check 실행

## Architecture

```text
Internet
  └─ Cloudflare Tunnel 또는 Caddy
       └─ Windows N100 + Ubuntu WSL2
            ├─ portal-web       Portal · File Manager · Status · Portfolio
            ├─ system-agent     Host metrics API
            ├─ homeops-executor Restricted Docker diagnostics · restart
            ├─ crawler-worker   RSS collection · archive · notification
            ├─ youtube-memo     Video notes
            └─ book-memo        Book notes

main push → GitHub Actions CI → Windows self-hosted runner → Docker Compose deploy
```

## Screenshots

| Portal | System Status |
|---|---|
| ![Portal dashboard](docs/images/portal-dashboard.png) | ![Admin status](docs/images/admin-status.png) |
| File Manager | News Hub |
| ![File manager](docs/images/file-manager.png) | ![News hub](docs/images/news-hub.png) |
| YouTube Memo | Book Memo |
| ![YouTube memo](docs/images/youtube-memo.png) | ![Book memo](docs/images/book-memo.png) |

## Tech Stack

- Backend: Python, FastAPI, Jinja2
- Storage: SQLite, JSON file storage
- Infrastructure: Docker Compose, Cloudflare, Caddy, Windows/WSL2
- CI/CD: GitHub Actions, Windows self-hosted runner

## Getting Started

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
```

운영 환경, 환경변수, N100 배포 절차는 [운영 문서](docs/README.md)를 참고하세요.

## Testing

GitHub Actions CI는 포털, 시스템 상태, 뉴스, HomeOps 실행기, 메모, Compose 및 운영 스크립트의 서비스별 테스트를 실행합니다. 로컬 테스트 명령은 [CI workflow](.github/workflows/ci.yml)를 기준으로 확인할 수 있습니다.

## Documentation

- [프로젝트 포트폴리오 원문](docs/portfolio-content.md)
- [운영 문서 색인](docs/README.md)
- [운영 참조](docs/operations-reference.md)
- [N100 운영 환경](docs/n100-mt4-setup.md)
- [N100 자동 배포](docs/n100-github-auto-deploy.md)

## AI-assisted Development

OpenAI Codex를 요구사항 분해, 테스트 작성, 구현 보조, 코드 검토 및 변경 검증에 활용했습니다. 서비스 공개 범위, 보안·운영 정책, 배포 여부와 최종 변경 판단은 작성자가 담당했습니다.
