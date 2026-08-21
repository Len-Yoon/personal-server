# 🏠 Personal Server

> N100 + WSL2 환경에서 운영하는 개인용 홈 서버

여러 서비스 링크, 파일 업로드, 서버 상태 확인, 뉴스, 유튜브·독서 메모를 한곳에서 관리하려고 만들었습니다. Docker Compose로 서비스별 컨테이너를 나눠 운영하고 있습니다.

단순히 화면만 모아둔 포털이 아니라, 파일·메모처럼 직접 수정하는 데이터와 서버 운영 기능을 분리해두는 쪽을 기준으로 잡았습니다. 평소에는 포털에서 필요한 화면으로 들어가고, 문제가 생기면 관리자 상태와 HomeOps 이력에서 먼저 확인하는 방식으로 사용합니다.

## ✨ 한눈에 보기

| 구분 | 내용 |
|---|---|
| 운영 환경 | Windows N100 + Ubuntu WSL2 |
| 서비스 구성 | Docker Compose 기반 서비스별 컨테이너 분리 |
| 배포 흐름 | `main` CI 통과 → N100 배포 → 서비스 health 검증 |
| 주요 기능 | 포털, 파일 관리, 시스템 상태, HomeOps, 뉴스, 메모, 포트폴리오 |

## 🧩 구성

| 서비스 | 하는 일 |
|---|---|
| 🧭 Portal | 자주 쓰는 서비스 링크와 뉴스·YouTube·책 메모를 모아 보는 시작 화면 |
| 📁 File Manager | 파일 여러 개 업로드, 폴더 생성, 검색·정렬, 보기 방식 변경, ZIP 다운로드 |
| 📊 System Status | N100 호스트의 CPU·메모리·디스크·백업·컨테이너 상태 확인 |
| 🛠️ HomeOps | Compose 서비스 상태 확인, 필요한 서비스 재시작, health check 결과와 실행 이력 관리 |
| 📰 News Hub | Investing.com·Google News RSS를 모으고, 나스닥 관련 기사를 분류해 중요한 내용만 Telegram 알림 |
| 📝 Memos | YouTube 영상의 타임스탬프 메모와 책·목차·독서 메모 관리 |
| 🪪 Portfolio | Markdown으로 작성한 포트폴리오 공개 및 로그인 후 편집 |

## 🔗 서비스 연결 방식

`portal-web`은 메인 화면, 파일함, 관리자 상태, 포트폴리오를 제공합니다. 나머지 서비스는 각자 SQLite 또는 파일 저장소를 사용하고, 포털은 링크와 검색 진입점 역할을 합니다. 따라서 뉴스 수집이나 메모 서비스에 문제가 생겨도 포털·파일함까지 같이 멈추지 않도록 구성했습니다.

운영 환경에서는 외부에 필요한 서비스만 HTTPS로 공개합니다. `system-agent`는 호스트 상태를 읽는 용도라 포털 컨테이너에서만 접근하고, `homeops-executor`는 Docker 내부 네트워크에서만 요청을 받습니다. 공개 HTTPS는 Cloudflare Tunnel 또는 Caddy + Cloudflare DNS-01 중 하나를 사용합니다.

## 🔐 운영 방식

- 뉴스는 시장 충격 가능성이 있는 기사와 전망성 기사를 구분해 알림 수를 줄였습니다.
- 상태 화면은 브라우저를 연 시간이 아니라 Windows 호스트에서 실제로 수집한 시각을 기준으로 표시합니다.
- 공개로 볼 수 있는 화면과 수정 권한이 필요한 화면을 분리했습니다. 수정 기능에는 세션 인증과 Origin 검증을 적용했습니다.
- `main` 브랜치 CI가 통과하면 Windows self-hosted runner가 N100에 배포하고 health check를 실행합니다.

### 💾 데이터와 보안

- 파일은 `data/files`에, 뉴스·YouTube 메모·책 메모·HomeOps 이력은 서비스별 데이터 경로에 보관합니다. 운영 데이터와 `.env`는 저장소에 올리지 않습니다.
- 관리자 상태, 파일함 접근·삭제, 메모 작성, 포트폴리오 편집은 각각 필요한 인증 경계를 둡니다. 인증 실패 제한은 재시작 후에도 유지되도록 관리합니다.
- HomeOps는 이 Compose 프로젝트의 컨테이너만 진단·재시작할 수 있습니다. 임의 명령 실행, 파일 삭제, Windows·WSL·Docker 엔진 재시작은 하지 않습니다.
- 재시작은 연속 health 실패 같은 조건에서만 시도하고, 재시작 후 health 확인·쿨다운·시간당 횟수 제한을 남깁니다. 필요한 상황만 Telegram으로 알립니다.

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

## 🖼️ Screenshots

| Portal | System Status |
|---|---|
| ![Portal dashboard](docs/images/portal-dashboard.png) | ![Admin status](docs/images/admin-status.png) |
| File Manager | News Hub |
| ![File manager](docs/images/file-manager.png) | ![News hub](docs/images/news-hub.png) |
| YouTube Memo | Book Memo |
| ![YouTube memo](docs/images/youtube-memo.png) | ![Book memo](docs/images/book-memo.png) |

## 🛠️ 사용 기술

| 영역 | 기술 |
|---|---|
| Backend | Python, FastAPI, Jinja2 |
| Storage | SQLite, JSON file storage |
| Infrastructure | Docker Compose, Cloudflare, Caddy, Windows/WSL2 |
| CI/CD | GitHub Actions, Windows self-hosted runner |

## 📁 디렉터리 구조

```text
portal-web/         메인 포털, 파일함, 관리자 상태, 포트폴리오
system-agent/       Windows 호스트 상태 조회 API
homeops-executor/   제한된 Docker 진단·재시작 실행기
crawler-worker/     RSS 수집, 분류, 뉴스 알림
youtube-memo/       YouTube 타임스탬프 메모
book-memo/          책·독서 메모
scripts/            N100 배포와 운영 보조 스크립트
docs/               운영 기준과 배포 문서
data/               운영 중 생성되는 파일·SQLite 데이터 (Git 제외)
```

## 🚀 로컬 실행

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
```

환경변수와 N100 배포 절차는 [운영 문서](docs/README.md)에 정리했습니다.

운영 배포는 `docker-compose.yml`에 N100용 override를 함께 적용합니다. N100에서는 서비스 포트를 localhost에만 바인드하고, CI 성공 후 self-hosted runner가 필요한 컨테이너를 재빌드·재기동합니다.

## 🧪 테스트

GitHub Actions CI에서 포털, 시스템 상태, 뉴스, HomeOps, 메모, Compose와 운영 스크립트의 서비스별 테스트를 돌립니다. 로컬에서 실행할 테스트는 [CI workflow](.github/workflows/ci.yml)를 보면 됩니다.

서비스마다 `app` 패키지명을 사용하므로 전체 테스트는 아래 실행기로 실행합니다. 각 스위트에 CI와 같은 `PYTHONPATH`를 적용하여 import 충돌을 방지합니다.

```bash
python3 tests/run_service_tests.py
```

특정 서비스만 확인하려면 `--suite`를 사용합니다. 사용할 수 있는 이름은 `python3 tests/run_service_tests.py --list`로 확인합니다.

## 📚 문서

- [프로젝트 포트폴리오 원문](docs/portfolio-content.md)
- [운영 문서 색인](docs/README.md)
- [운영 참조](docs/operations-reference.md)
- [N100 운영 환경](docs/n100-mt4-setup.md)
- [N100 자동 배포](docs/n100-github-auto-deploy.md)

## 🤖 Codex 작업 완료 루프

이 저장소는 OpenAI Codex 작업 완료 루프를 문서화하고, 변경 범위 분류·서비스별 CI 검증·PR 정책 검토 결과를 작업 artifact로 기록합니다. CI artifact와 로그는 90일 보존하며, 장기 보관이 필요한 증거는 만료 전에 문서 또는 별도 증적 저장소로 보관합니다. 일반 개발 작업은 주·구현·검토 역할의 최대 3명으로 수행하며, CI/CD·보안·DB 마이그레이션·배포 영향이 있는 경우에만 전문 검토 역할을 추가해 최대 4명으로 운영합니다. 브랜치를 사용한 작업은 PR 병합 또는 중단 후 정리 여부를 확인하고, 미병합·재사용 예정 브랜치는 유지합니다. 이 기능은 자동 코드 수정, 자동 merge 또는 새로운 에이전트 주도 배포를 추가하지 않습니다. 기존 `main` 브랜치 CI 성공을 트리거로 한 N100 배포는 기존 게이트를 따릅니다. 차단되거나 분류되지 않은 변경과 검증 실패는 검토를 위해 중단합니다. 자세한 기준은 [Codex 작업 완료 루프 증거 운영](docs/agent-loop-evidence.md)에서 확인할 수 있습니다.
