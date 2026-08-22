# 🏠    Personal Server

> Windows N100 + WSL2에서 운영하는 개인용 홈 서버

포털, 파일 관리, 서버 상태, 뉴스, YouTube·책 메모를 한곳에서 관리하는 Docker Compose 기반 개인 서버입니다.

파일·메모처럼 직접 수정하는 데이터와 서버 운영 기능을 분리했습니다. 평소에는 포털에서 필요한 서비스로 이동하고, 상태 문제는 관리자 화면과 HomeOps 이력에서 먼저 확인합니다.

## ✨ 한눈에 보기

| 구분 | 내용 |
|---|---|
| 운영 환경 | Windows N100 + Ubuntu WSL2 |
| 서비스 구성 | Docker Compose 기반 서비스별 컨테이너 분리 |
| 배포 흐름 | `main` CI 통과 → N100 배포 → 서비스 health 검증 |
| 주요 기능 | 포털, 파일 관리, 시스템 상태, HomeOps, 뉴스, 메모, 포트폴리오 |

<br>

## 🚀 빠른 시작

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
```

N100 운영 환경, 환경변수, 배포 절차는 [운영 문서 색인](docs/README.md)을 기준으로 확인합니다.

<br>

## 🧩 무엇을 할 수 있나

| 서비스 | 하는 일 |
|---|---|
| 🧭 Portal | 자주 쓰는 서비스 링크와 뉴스·YouTube·책 메모를 모아 보는 시작 화면 |
| 📁 File Manager | 파일 여러 개 업로드, 폴더 생성, 검색·정렬, 보기 방식 변경, ZIP 다운로드 |
| 📊 System Status | N100 호스트의 CPU·메모리·디스크·백업·컨테이너 상태 확인 |
| 🛠️ HomeOps | Compose 서비스 상태 확인, 필요한 서비스 재시작, health check 결과와 실행 이력 관리 |
| 📰 News Hub | Investing.com·Google News RSS를 모으고, 나스닥 관련 기사를 분류해 중요한 내용만 Telegram 알림 |
| 📝 Memos | YouTube 영상의 타임스탬프 메모와 책·목차·독서 메모 관리 |
| 🪪 Portfolio | Markdown으로 작성한 포트폴리오 공개 및 로그인 후 편집 |

`portal-web`은 포털, 파일함, 관리자 상태, 포트폴리오를 제공합니다. 뉴스와 메모 서비스는 각자 SQLite 또는 파일 저장소를 사용하며, 포털은 링크와 검색 진입점 역할을 합니다.

<br>

## 🔐 운영과 보안

- 공개 화면과 수정 권한이 필요한 화면을 분리하고, 수정 기능에는 세션 인증과 Origin 검증을 적용합니다.
- 파일은 `data/files`에, 뉴스·메모·HomeOps 이력은 서비스별 데이터 경로에 보관합니다. 운영 데이터와 `.env`는 저장소에 올리지 않습니다.
- `system-agent`는 포털 컨테이너에서만 접근하고, `homeops-executor`는 Docker 내부 네트워크에서만 요청을 받습니다.
- HomeOps는 이 Compose 프로젝트의 컨테이너만 진단·재시작합니다. 임의 명령 실행, 파일 삭제, Windows·WSL·Docker 엔진 재시작은 수행하지 않습니다.
- 뉴스는 시장 충격 가능성이 있는 기사와 전망성 기사를 구분해 알림 수를 줄입니다.

<br>

## 🏗️ Architecture

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

공개 HTTPS는 Cloudflare Tunnel 또는 Caddy + Cloudflare DNS-01 중 실제 환경에 맞는 한 가지 방식만 사용합니다.

<br>

## 🖼️ Screenshots

| Portal | System Status |
|---|---|
| ![Portal dashboard](docs/images/portal-dashboard.png) | ![Admin status](docs/images/admin-status.png) |
| File Manager | News Hub |
| ![File manager](docs/images/file-manager.png) | ![News hub](docs/images/news-hub.png) |
| YouTube Memo | Book Memo |
| ![YouTube memo](docs/images/youtube-memo.png) | ![Book memo](docs/images/book-memo.png) |

<br>

## ✅ 검증

GitHub Actions CI에서 포털, 시스템 상태, 뉴스, HomeOps, 메모, Compose와 운영 스크립트의 서비스별 테스트를 실행합니다.

```bash
python3 tests/run_service_tests.py
```

특정 서비스만 확인하려면 `--suite`를 사용합니다. 사용할 수 있는 이름은 아래 명령으로 확인합니다.

```bash
python3 tests/run_service_tests.py --list
```

<br>

## 🛠️ 사용 기술

| 영역 | 기술 |
|---|---|
| Backend | Python, FastAPI, Jinja2 |
| Storage | SQLite, JSON file storage |
| Infrastructure | Docker Compose, Cloudflare, Caddy, Windows/WSL2 |
| CI/CD | GitHub Actions, Windows self-hosted runner |

<br>

## 🔎 더 알아보기

| 문서 | 내용 |
|---|---|
| [운영 문서 색인](docs/README.md) | 운영 문서의 단일 진입점 |
| [운영 참조](docs/operations-reference.md) | 도메인·환경변수·일상 점검 명령 |
| [N100 운영 환경](docs/n100-mt4-setup.md) | Windows·WSL2·Docker 운영 |
| [N100 자동 배포](docs/n100-github-auto-deploy.md) | GitHub Actions 배포와 장애 대응 |
| [프로젝트 포트폴리오 원문](docs/portfolio-content.md) | 공개 포트폴리오용 프로젝트 설명 |

<br>

## 🤖 Codex 작업 완료 루프

- 변경 범위 분류, 서비스별 CI 검증, PR 정책 검토 결과를 artifact로 기록합니다.
- CI artifact와 로그는 90일 보존하며, 장기 보관이 필요한 증거는 만료 전에 문서 또는 별도 증적 저장소로 옮깁니다.
- 병합된 PR은 CI·배포 성공 및 작업공간 무변경을 확인한 뒤 정리합니다. 미병합·재사용 예정 브랜치는 유지합니다.
- 차단·미분류 변경과 검증 실패는 검토를 위해 중단합니다.

<br>

자세한 작업 절차는 [Codex 작업 완료 루프](docs/codex-work-loop.md), artifact 확인은 [작업 루프 증거 운영](docs/agent-loop-evidence.md)을 참고합니다.
