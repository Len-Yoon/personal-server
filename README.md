# 🏠 Personal Server

> Windows N100 + WSL2에서 실제 운영 중인 개인용 홈 서버

<br>

개인 데이터 관리, 뉴스·메모, 서버 운영, 차량관리를 하나의 Docker Compose 환경에서 운영합니다
기능 구현은 기능 브랜치·PR CI·독립 검토·`main` 자동배포·health 검증까지 연결해 관리합니다.

<br>

## ✨ 한눈에 보기

| 항목 | 내용 |
|---|---|
| 운영 환경 | Windows N100 + Ubuntu WSL2 |
| 서비스 구조 | Docker Compose 기반 서비스별 컨테이너 분리 |
| 대표 기능 | 포털·파일 관리·뉴스·메모·서버 상태·차량관리 Telegram |
| 운영 흐름 | 기능 브랜치 → PR CI·독립 검토 → `main` CI → N100 배포·health 검증 |

<br>

## 🖼️ 주요 화면

| 포털 대시보드 | 차량관리 Telegram |
|---|---|
| <img src="docs/images/portal-dashboard.png" alt="Personal Server Portal dashboard" width="360"> | <img src="docs/images/car-care-telegram-status.png" alt="Telegram 차량관리 최신 운행 결과 알림" width="360"> |

| File Manager | News Hub |
|---|---|
| <img src="docs/images/file-manager.png" alt="File manager" width="360"> | <img src="docs/images/news-hub.png" alt="News hub" width="360"> |

| YouTube Memo | Book Memo |
|---|---|
| <img src="docs/images/youtube-memo.png" alt="YouTube memo" width="360"> | <img src="docs/images/book-memo.png" alt="Book memo" width="360"> |

<br>

## 🧩 핵심 기능

### 🧭 포털과 개인 데이터

`portal-web`은 자주 쓰는 링크, 파일함, 서버 상태, 포트폴리오를 한 화면에서 연결합니다.

<br>

#### 🗂️ 파일과 기록

- **File Manager**: 다중 업로드, 폴더 생성, 검색·정렬, ZIP 다운로드
- **YouTube / Book Memos**: 영상 타임스탬프와 독서 기록 관리

<br>

#### 공개 포트폴리오

- **Portfolio**: Markdown 기반 공개 포트폴리오와 로그인 후 편집

### 📰 뉴스와 알림

`crawler-worker`는 Investing.com·Google News RSS를 수집하고, 나스닥 관련 기사를 분류해 Telegram으로 보냅니다.

<br>

시장 충격 가능성이 있는 기사와 전망성 기사를 구분해 불필요한 알림을 줄입니다.

<br>

### 🚗 차량관리 Telegram

`car-care-worker`는 Hyundai OAuth 연동 후 누적 주행거리, 주행 가능 거리, 차량 경고 상태를 수집합니다. 운행 종료를 감지하면 이번 운행 거리와 다음 정비 잔여 거리를 Telegram으로 보냅니다.

<br>

#### 주행 상태와 안전 알림

| 자동화 항목 | 현재 동작 |
|---|---|
| 운행 종료 요약 | 운행 거리, 누적 주행거리, 주행 가능 거리, 엔진오일 잔여 거리 알림 |
| 저유·경고 상태 | 주행 가능 거리 100km·50km 알림, 경고등 점등·정상 복귀 알림 |

<br>

#### 정비와 계절 관리

| 자동화 항목 | 현재 동작 |
|---|---|
| 정비 주기 | 엔진오일 10,000km, 미션오일·연료필터 60,000km / 각 500km 전부터 알림 |
| 계절 타이어 | 매년 11월 15일 윈터타이어, 4월 1일 사계절타이어 교체 알림 |

<br>

#### 수동 보정과 연결 설정

| 자동화 항목 | 현재 동작 |
|---|---|
| 수동 보정 | Hyundai 연동이 없을 때 `/주행거리 <km>`, `/정비완료` 명령으로 관리 |

<details>
<summary><strong>차량관리 명령어와 Hyundai 연결 설정</strong></summary>

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

`.env`에는 `CAR_CARE_TELEGRAM_BOT_TOKEN`, `CAR_CARE_TELEGRAM_CHAT_ID`, `HYUNDAI_CLIENT_ID`, `HYUNDAI_CLIENT_SECRET`, `HYUNDAI_REDIRECT_URI`를 설정합니다.

Hyundai OAuth 콜백은 `car.len.pe.kr`에서 Cloudflare Tunnel을 통해 로컬 `8015` 포트로 전달합니다. 차량 상태는 `data/car-care`, OAuth 토큰은 별도 Docker volume에 저장합니다.

</details>

<br>

### 🛠️ 서버 운영과 HomeOps

#### 상태 진단

System Status는 N100 호스트의 CPU·메모리·디스크·백업·컨테이너 상태를 확인합니다.

<br>

#### 전체 재시작

HomeOps는 관리 대상 서비스를 한 번에 진단해 정상·비정상 서비스와 비정상 이유만 간결히 보여줍니다. 필요하면 전체 재시작을 실행하며, 개별 서비스 실패가 나도 나머지 서비스 처리는 계속합니다.

<br>

> 전체 재시작에는 포털도 포함됩니다. 실행 후 화면 연결이 잠시 끊길 수 있으므로 약 20~30초 뒤 관리자 상태 페이지를 다시 열어 결과를 확인합니다.

<br>

## 🤖 Harness / Loop Engineering

### 개발·검증 루프

AI가 코드를 작성하는 것에서 끝내지 않고, 운영 반영까지 확인 가능한 개발 루프를 유지합니다.

<br>

```text
요구사항·성공 기준
        ↓
기능 브랜치 구현 · 회귀 테스트
        ↓
독립 검토 · PR CI · Agent Review
        ↓
사용자 병합 승인
        ↓
main CI · N100 자동배포 · health 검증
```

<br>

### 운영 증거와 배포 통제

| 단계 | 적용 방식 |
|---|---|
| 범위 관리 | 변경·제외 범위와 성공 기준을 먼저 정의 |
| 품질 확인 | 기능 테스트와 독립 diff 검토를 분리 |
| 배포 통제 | PR CI 통과와 사용자 병합 승인 후 `main` 반영 |
| 운영 검증 | N100 배포 후 컨테이너·공개 서비스 health 확인 |
| 증거 보존 | CI artifact·로그 90일 보존, 장기 보관이 필요한 증적은 별도 저장소로 이전 |

<br>

세부 절차는 [Codex 작업 완료 루프](docs/codex-work-loop.md), 증거 운영은 [작업 루프 증거 운영](docs/agent-loop-evidence.md)을 참고합니다.

<br>

## 🚀 빠른 시작

### 로컬 실행

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
```

<br>

### 운영 환경 참고

N100 운영 환경, 환경변수, 배포 절차는 [운영 문서 색인](docs/README.md)에서 확인합니다.

<br>

## ✅ 검증

GitHub Actions CI에서 포털, 시스템 상태, 뉴스, HomeOps, 메모, 차량관리, Compose와 운영 스크립트의 서비스별 테스트를 실행합니다.

<br>

```bash
python3 tests/run_service_tests.py
python3 tests/run_service_tests.py --list
```

특정 서비스만 확인하려면 `--suite`를 사용합니다.

<br>

## 🏗️ 아키텍처

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
```

공개 HTTPS는 Cloudflare Tunnel 또는 Caddy + Cloudflare DNS-01 중 실제 환경에 맞는 한 가지 방식만 사용합니다.

<br>

## 🔎 운영 문서

| 문서 | 내용 |
|---|---|
| [운영 문서 색인](docs/README.md) | 운영 문서의 단일 진입점 |
| [운영 참조](docs/operations-reference.md) | 도메인·환경변수·일상 점검 명령 |
| [N100 운영 환경](docs/n100-mt4-setup.md) | Windows·WSL2·Docker 운영 |
| [N100 자동 배포](docs/n100-github-auto-deploy.md) | GitHub Actions 배포와 장애 대응 |
| [프로젝트 포트폴리오 원문](docs/portfolio-content.md) | 공개 포트폴리오용 프로젝트 설명 |
