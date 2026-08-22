# 🏠 Personal Server

> Windows N100 + WSL2에서 운영하는 개인용 홈 서버

포털, 파일 관리, 서버 상태, 뉴스, YouTube·책 메모를 한곳에서 관리하는 Docker Compose 기반 개인 서버입니다.

파일·메모처럼 직접 수정하는 데이터와 서버 운영 기능을 분리했습니다.

평소에는 포털에서 필요한 서비스로 이동하고, 상태 문제는 관리자 화면과 HomeOps 이력에서 먼저 확인합니다.

## ✨ 프로젝트 개요

| 구분 | 내용 |
|---|---|
| 운영 환경 | Windows N100 + Ubuntu WSL2 |
| 서비스 구성 | Docker Compose 기반 서비스별 컨테이너 분리 |
| 배포 흐름 | PR 병합 → `main` CI → N100 배포 → 서비스 health 검증 |
| 개발 방식 | Harness / Loop Engineering 기반 검증·CI·PR 배포 루프 |
| 주요 기능 | 포털, 파일 관리, 시스템 상태, HomeOps, 뉴스, 메모, 차량관리, 포트폴리오 |

## 🚀 빠른 시작

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
```

N100 운영 환경, 환경변수, 배포 절차는 [운영 문서 색인](docs/README.md)을 기준으로 확인합니다.

## 🧩 서비스 구성

### 사용자 서비스

| 서비스 | 하는 일 |
|---|---|
| 🧭 Portal | 자주 쓰는 서비스 링크와 뉴스·YouTube·책 메모를 모아 보는 시작 화면 |
| 📁 File Manager | 파일 여러 개 업로드, 폴더 생성, 검색·정렬, 보기 방식 변경, ZIP 다운로드 |
| 📊 System Status | N100 호스트의 CPU·메모리·디스크·백업·컨테이너 상태 확인 |
| 📰 News Hub | Investing.com·Google News RSS를 모으고, 나스닥 관련 기사를 분류해 중요한 내용만 Telegram 알림 |
| 📝 Memos | YouTube 영상의 타임스탬프 메모와 책·목차·독서 메모 관리 |
| 🪪 Portfolio | Markdown으로 작성한 포트폴리오 공개 및 로그인 후 편집 |

### 운영·자동화 서비스

| 서비스 | 하는 일 |
|---|---|
| 🛠️ HomeOps | Compose 서비스 상태 확인, 필요한 서비스 재시작, health check 결과와 실행 이력 관리 |
| 🚗 Car Care | Telegram으로 주행거리·정비 이력·정비 알림·차량 경고·운행 종료 요약 관리 |

`portal-web`은 포털, 파일함, 관리자 상태, 포트폴리오를 제공합니다.

뉴스와 메모 서비스는 각자 SQLite 또는 파일 저장소를 사용하며, 포털은 링크와 검색 진입점 역할을 합니다.

## 🔐 운영과 보안

### 접근 제어

- 공개 화면과 수정 권한이 필요한 화면을 분리하고, 수정 기능에는 세션 인증과 Origin 검증을 적용합니다.
- `system-agent`는 포털 컨테이너에서만 접근하고, `homeops-executor`는 Docker 내부 네트워크에서만 요청을 받습니다.
- HomeOps는 이 Compose 프로젝트의 컨테이너만 진단·재시작합니다. 임의 명령 실행, 파일 삭제, Windows·WSL·Docker 엔진 재시작은 수행하지 않습니다.

### 데이터와 알림

- 파일은 `data/files`에, 뉴스·메모·HomeOps 이력은 서비스별 데이터 경로에 보관합니다. 운영 데이터와 `.env`는 저장소에 올리지 않습니다.
- 뉴스는 시장 충격 가능성이 있는 기사와 전망성 기사를 구분해 알림 수를 줄입니다.

## 🏗️ 아키텍처와 배포

```text
Internet
  └─ Cloudflare Tunnel 또는 Caddy
       └─ Windows N100 + Ubuntu WSL2
            ├─ portal-web       Portal · File Manager · Status · Portfolio
            ├─ system-agent     Host metrics API
            ├─ homeops-executor Restricted Docker diagnostics · restart
            ├─ crawler-worker   RSS collection · archive · notification
            ├─ youtube-memo     Video notes
            ├─ book-memo        Book notes
            └─ car-care-worker  Telegram vehicle-care worker (internal only)

feature branch → PR CI · Agent Review → main CI → N100 Docker Compose deploy
```

공개 HTTPS는 Cloudflare Tunnel 또는 Caddy + Cloudflare DNS-01 중 실제 환경에 맞는 한 가지 방식만 사용합니다.

## 🚗 차량관리 Telegram

`car-care-worker`는 Telegram long polling으로 동작합니다.

Hyundai OAuth 완료를 위해 `car.len.pe.kr`의 콜백 경로만 Cloudflare Tunnel에서 로컬 `8015` 포트로 전달하며, Caddy 경로는 사용하지 않습니다.

| 구분 | 내용 |
|---|---|
| Hyundai 연동 | `/현대연결`로 로그인·데이터 제공 동의를 완료하면 주행거리, 주행 가능 거리, 차량 경고를 조회함 |
| 정비 알림 | 엔진오일 10,000km, 미션오일·연료필터 60,000km 주기이며 각 항목은 500km 전부터 알림 |
| 계절 알림 | 매년 11월 15일 윈터타이어, 4월 1일 사계절타이어 교체 알림 |
| 운행 요약 | 주행거리 변화가 멈춘 뒤 운행 거리·주행 가능 거리·엔진오일 잔여 거리 알림 |

### Telegram 명령어

```text
/차량
/정비완료 엔진오일 [km]
/정비완료 미션오일 [km]
/정비완료 연료필터 [km]
/타이어교체 윈터
/타이어교체 사계절
/정비목록
/알림테스트
/현대연결
```

`.env`에는 `CAR_CARE_TELEGRAM_BOT_TOKEN`, `CAR_CARE_TELEGRAM_CHAT_ID`, `HYUNDAI_CLIENT_ID`, `HYUNDAI_CLIENT_SECRET`, `HYUNDAI_REDIRECT_URI=https://car.len.pe.kr/oauth/hyundai/callback`을 설정합니다.

SQLite 상태는 `data/car-care`에, OAuth 토큰은 Docker named volume `car-care-oauth`의 `/data/oauth`에 분리 저장됩니다.

Hyundai 연동은 선택 사항이며, 미설정 시 `/주행거리 <km>`로 수동 관리할 수 있습니다.

<p align="center">
  <img src="docs/images/car-care-telegram-status.png" alt="Telegram 차량관리 봇의 차량 상태와 정비 잔여 거리 예시" width="360">
</p>

## 🖼️ 주요 화면

### Portal · System Status

| Portal | System Status |
|---|---|
| ![Portal dashboard](docs/images/portal-dashboard.png) | ![Admin status](docs/images/admin-status.png) |

### File Manager · News Hub

| File Manager | News Hub |
|---|---|
| ![File manager](docs/images/file-manager.png) | ![News hub](docs/images/news-hub.png) |

### Memos

| YouTube Memo | Book Memo |
|---|---|
| ![YouTube memo](docs/images/youtube-memo.png) | ![Book memo](docs/images/book-memo.png) |

## ✅ 검증

GitHub Actions CI에서 포털, 시스템 상태, 뉴스, HomeOps, 메모, 차량관리, Compose와 운영 스크립트의 서비스별 테스트를 실행합니다.

```bash
python3 tests/run_service_tests.py
python3 tests/run_service_tests.py --list
```

특정 서비스만 확인하려면 `--suite`를 사용합니다. 사용할 수 있는 이름은 `--list`로 확인합니다.

## 🤖 Harness / Loop Engineering

AI 보조 개발에서도 코드 생성에만 의존하지 않고, 요구사항부터 운영 반영까지 검증 가능한 개발 루프를 적용합니다.

### 개발 루프

- 변경 범위·제외 범위·성공 기준을 먼저 정의하고 기능 브랜치에서 작업합니다.
- 기능별 회귀 테스트, 서비스별 CI, 변경 범위 정책을 함께 검증합니다.
- 보안·운영·시간대·중복 발송처럼 놓치기 쉬운 조건은 독립 검토로 확인합니다.
- PR CI·Agent Review를 통과하고 사용자 병합 승인을 받은 변경만 `main` 배포 대상으로 사용합니다.
- CI artifact와 로그는 90일 보존하며, 장기 보관이 필요한 증거는 별도 증적 저장소로 옮깁니다. 병합된 PR은 CI·배포 성공 및 작업공간 무변경을 확인한 뒤 정리합니다.

자세한 작업 절차는 [Codex 작업 완료 루프](docs/codex-work-loop.md), artifact 확인은 [작업 루프 증거 운영](docs/agent-loop-evidence.md)을 참고합니다.

## 🛠️ 사용 기술

| 영역 | 기술 |
|---|---|
| Backend | Python, FastAPI, Jinja2 |
| Storage | SQLite, JSON file storage |
| Infrastructure | Docker Compose, Cloudflare, Caddy, Windows/WSL2 |
| CI/CD | GitHub Actions, Windows self-hosted runner |

## 🔎 운영 문서

| 문서 | 내용 |
|---|---|
| [운영 문서 색인](docs/README.md) | 운영 문서의 단일 진입점 |
| [운영 참조](docs/operations-reference.md) | 도메인·환경변수·일상 점검 명령 |
| [N100 운영 환경](docs/n100-mt4-setup.md) | Windows·WSL2·Docker 운영 |
| [N100 자동 배포](docs/n100-github-auto-deploy.md) | GitHub Actions 배포와 장애 대응 |
| [프로젝트 포트폴리오 원문](docs/portfolio-content.md) | 공개 포트폴리오용 프로젝트 설명 |
