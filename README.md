# Personal Server

개인 생산성과 홈 서버 운영에 필요한 기능을 하나의 웹 플랫폼으로 통합한 개인 프로젝트임. Windows N100과 WSL2 환경에서 Docker Compose 기반으로 운영하며, 포털·파일함·운영 현황·뉴스·메모·공개 포트폴리오를 서비스별 책임으로 분리해 구현함.

- 공개 포털: [len.pe.kr](https://len.pe.kr)
- 공개 포트폴리오: [portfolio.len.pe.kr](https://portfolio.len.pe.kr)
- 구현 기간: 2026년 7월~현재

## 프로젝트 요약

| 항목 | 내용 |
|---|---|
| 해결 과제 | 흩어진 개인 도구, 과도한 시장 뉴스 알림, 판단 근거가 불명확한 서버 상태 정보를 통합 관리할 필요가 있었음 |
| 역할 | 문제 정의, 기능 우선순위, 보안 정책, 운영 구조, 배포 기준 및 최종 변경 판단을 담당함 |
| 구현 범위 | 포털, 파일함, 관리자 상태, HomeOps 운영 보조, 뉴스 허브·알림, YouTube·책 메모, Markdown 기반 포트폴리오 |
| 운영 환경 | Windows N100, Ubuntu-24.04 WSL2, Docker Compose, Cloudflare Tunnel 또는 Caddy |
| 검증 방식 | 서비스별 단위 테스트, 브라우저 동작 계약 테스트, Compose 설정 테스트, GitHub Actions CI, 배포 후 health 확인 |

## 핵심 성과

### 1. 개인 서비스를 단일 포털과 도메인별 서비스로 통합

- 포털에서 파일함, 관리자 상태, 뉴스, YouTube 메모, 책 메모를 연결하고 통합 검색을 제공함.
- 공개 포트폴리오는 별도 호스트에서 제공하고, 편집 기능은 인증된 관리자 경로로 분리함.
- N100 운영 환경에서는 앱 포트를 `127.0.0.1`로 제한하고, 외부 요청은 Cloudflare Tunnel 또는 Caddy를 경유하도록 구성함.

### 2. 시장 영향도 기반 뉴스 알림 정책 구현

- Investing.com RSS와 Google News RSS를 수집·보관하는 뉴스 허브를 구현함.
- 나스닥 관련성 및 시장 충격 여부를 분류하여, 확정된 중요 기사만 Telegram으로 알림 처리함.
- 금리 전망성 기사가 즉시 알림으로 분류되지 않도록 재현 테스트를 추가해 정책 회귀를 방지함.

### 3. 실제 수집 시각을 기준으로 한 서버 상태와 제한된 운영 보조 구현

- Windows host가 기록한 `captured_at`을 기준으로 CPU·메모리·디스크·백업·컨테이너 상태를 표시함.
- HomeOps는 Compose 서비스만 진단하며, 정해진 조건에서만 컨테이너 재시작을 허용하도록 권한과 대상을 제한함.
- 재시작 후 health 검증, 서비스별 쿨다운, 시간당 실행 한도, SQLite 이력을 적용해 운영 조치의 근거를 남김.

### 4. 공개 서비스의 쓰기 경로 보안 강화

- 관리자·파일함·메모 쓰기·포트폴리오 편집을 역할별 세션 인증으로 분리함.
- Origin 검증, HTTP-only·SameSite 쿠키, CSP 및 보안 헤더, 영속형 인증 실패 제한을 적용함.
- 파일함에는 경로 이탈 방지, 위험 확장자 차단, 용량 제한, 덮어쓰기 방지와 일괄 다운로드를 구현함.

### 5. 테스트 게이트 기반 N100 자동 배포 구성

- GitHub Actions CI에서 포털, 시스템 상태, 뉴스, HomeOps 실행기, 메모, Compose·운영 스크립트의 테스트를 서비스별로 실행함.
- `main` 브랜치의 CI가 성공한 경우에만 Windows self-hosted runner가 N100에서 배포 스크립트를 실행하도록 구성함.
- 배포 후 Compose 실행 상태와 각 서비스 health endpoint를 확인해 실패를 workflow 결과로 남김.

## 문제 해결 사례

| 문제 | 의사결정 | 구현 및 검증 |
|---|---|---|
| 중요하지 않은 뉴스까지 알림될 가능성 | 시장 충격 기사와 전망성 기사를 분리함 | RSS 수집·관련성 분류·알림을 분리하고, 전망성 기사 오탐 방지 테스트를 작성함 |
| 웹 파일함의 조작성이 낮음 | 기존 파일 처리 계약을 유지하며 탐색 경험을 개선함 | 다중 업로드, 폴더 생성, 검색·정렬, 아이콘/목록 보기, 키보드 선택, ZIP 일괄 다운로드를 구현함 |
| 화면 조회 시각과 서버 상태 수집 시각이 혼동됨 | host가 기록한 실제 수집 시각을 상태 기준으로 사용함 | `captured_at`과 오래됨 상태를 함께 표시하고 관련 화면 계약을 검증함 |
| 공개 쓰기 기능의 공격 표면 | 읽기 공개와 쓰기 인증을 분리하고 요청 출처를 검증함 | 교차 출처 요청 차단, 로그인 실패 제한의 재시작 후 유지, 세션 기반 쓰기 흐름을 테스트함 |

## 아키텍처

```text
Internet
  └─ Cloudflare DNS / Tunnel 또는 Caddy
       └─ N100 Windows + Ubuntu WSL2
            ├─ portal-web       포털·파일함·관리자 상태·포트폴리오
            ├─ system-agent     Windows host metrics 상태 API
            ├─ homeops-executor 제한된 Docker 진단·재시작 실행
            ├─ crawler-worker   RSS 수집·뉴스 보관·Telegram 알림
            ├─ youtube-memo     영상·학습 메모
            └─ book-memo        책·목차·독서 메모

GitHub main push → CI 성공 → Windows self-hosted runner → N100 Docker Compose 배포
```

## 화면 예시

| 포털 | 관리자 상태 |
|---|---|
| ![포털 화면](docs/images/portal-dashboard.png) | ![관리자 상태](docs/images/admin-status.png) |
| 파일함 | 뉴스 허브 |
| ![파일함](docs/images/file-manager.png) | ![뉴스 허브](docs/images/news-hub.png) |
| YouTube 메모 | 책 메모 |
| ![YouTube 메모](docs/images/youtube-memo.png) | ![책 메모](docs/images/book-memo.png) |

## 기술 스택

`Python` · `FastAPI` · `Jinja2` · `SQLite` · `Docker Compose` · `Cloudflare` · `Caddy` · `GitHub Actions` · `Windows/WSL2`

## AI 협업 방식

OpenAI Codex는 요구사항 분해, 테스트 작성, 구현 보조, 독립 검토 및 변경 검증에 활용함. 기능 우선순위, 서비스 공개 범위, 인증·운영 정책, 배포 여부와 최종 `main` 반영은 작성자가 결정함. 외부 AI API를 서비스 기능으로 연동하지 않았으며, 뉴스와 HomeOps의 실행 판단은 코드에 정의된 규칙 기반 정책으로 처리함.

## 문서

- [공개 포트폴리오 Markdown 초안](docs/portfolio-content.md)
- [운영 문서 색인](docs/README.md)
- [운영 참조](docs/operations-reference.md)
- [N100 운영 환경](docs/n100-mt4-setup.md)
- [N100 GitHub 자동 배포](docs/n100-github-auto-deploy.md)
- [Cloudflare Tunnel](docs/cloudflare-tunnel.md)
- [Caddy + Cloudflare HTTPS](docs/caddy-cloudflare.md)
- [개발 인수인계](docs/agent-handoff.md)
