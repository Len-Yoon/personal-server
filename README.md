# Personal Server

**개인 서버 운영에서 흩어진 도구, 과도한 뉴스 알림, 불명확한 서버 상태를 해결하기 위해 직접 설계·구현한 서비스 플랫폼임.** OpenAI Codex는 요구사항 분해, TDD, 역할 분리 검토, 증적 기반 병합에 활용했으며, 기능 우선순위·보안 정책·운영 구조와 최종 병합 판단은 작성자가 결정함.

개인 생산성과 서버 운영을 위해 구축한 Docker Compose 기반 서비스 플랫폼임. 포털, 파일함, 서버 상태, 나스닥 뉴스 알림, YouTube·책 메모, 공개 포트폴리오를 하나의 시스템으로 운영함.

공개 포털: [https://len.pe.kr](https://len.pe.kr)

## 핵심 성과

- 개인용 서비스를 단일 포털과 서브도메인 기반으로 통합함.
- Investing.com RSS를 나스닥 관점으로 분류하고, 중요 기사만 텔레그램으로 즉시 알림 처리함.
- Windows 탐색기 형태의 웹 파일함에 업로드 정책, 폴더 관리, 일괄 다운로드, 키보드 조작을 구현함.
- N100 Windows host의 실제 수집 시각을 기반으로 서버·백업·컨테이너 상태를 시각화함.
- 공개 서비스의 쓰기 경로에 세션 인증, CSRF Origin 검증, CSP, 영속형 인증 실패 제한을 적용함.
- `main` push부터 N100 반영까지 GitHub Actions self-hosted runner 기반 자동 배포를 구성함.
- 관리자 상태에 HomeOps 운영 보조를 통합해 Compose 서비스 진단, 제한 재시작, 복구 검증, KST 기준 장애 이력 및 Telegram 상태 변화 알림을 구현함.

## 문제 해결 사례

| 문제 | 작성자 의사결정 | 구현·검증 결과 |
|---|---|---|
| 나스닥 관련 뉴스가 너무 적거나, 중요하지 않은 기사까지 알림이 될 수 있음 | 시장 영향도 기준으로 분류하고, 확정된 시장 충격 기사만 즉시 Telegram 알림. 전망성 기사는 보관함으로 분리함 | RSS 수집·관련성 분류·Telegram 알림을 분리하고, 전망성 기사 오탐을 재현 테스트로 차단함 |
| 웹 파일함이 일반 파일 탐색기보다 조작이 불편함 | 업로드·다운로드·삭제 계약은 유지하면서 검색, 정렬, 아이콘/목록 보기, 키보드 선택을 추가함 | 기존 파일 처리 기능 회귀를 유지하고 브라우저 동작 계약 테스트를 추가함 |
| 관리자 화면이 실제 수집 시각 대신 화면 접속 시각처럼 보임 | Windows host가 기록한 `captured_at`을 상태 데이터의 기준 시각으로 사용하고, 오래된 데이터는 별도 상태로 표시함 | CPU·메모리·디스크와 실제 host 수집 시각을 함께 표시해 상태 판단 기준을 분리함 |
| 공개 메모 서비스의 쓰기·삭제 경로가 공격 표면이 될 수 있음 | 읽기는 공개, 쓰기는 세션 인증으로 분리하고 Origin 검증·보안 헤더·영속형 인증 실패 제한을 적용함 | 교차 출처 쓰기 차단, 재시작 후에도 유지되는 인증 실패 제한, 로그인 세션 기반 삭제 흐름을 테스트로 검증함 |

## 화면 예시

| 포털 | 관리자 상태 |
|---|---|
| ![포털 화면](docs/images/portal-dashboard.png) | ![관리자 상태](docs/images/admin-status.png) |
| 파일함 | 뉴스 허브 |
| ![파일함](docs/images/file-manager.png) | ![뉴스 허브](docs/images/news-hub.png) |
| YouTube 메모 | 책 메모 |
| ![YouTube 메모](docs/images/youtube-memo.png) | ![책 메모](docs/images/book-memo.png) |

## 아키텍처

```text
Internet
  └─ Cloudflare DNS / Tunnel
       └─ N100 Windows의 WSL cloudflared
            ├─ portal-web      : 포털, 파일함, 관리자 상태, 포트폴리오
            ├─ homeops-executor: 제한된 Docker 진단·컨테이너 재시작
            ├─ crawler-worker  : Investing.com RSS, 뉴스 보관, 텔레그램 알림
            ├─ youtube-memo    : 영상·학습 메모
            └─ book-memo       : 책·목차·독서 메모

N100 Windows host
  └─ host-metrics.json → system-agent → 관리자 상태 화면

GitHub main push
  └─ GitHub Actions self-hosted runner → N100 Docker Compose 재배포
```

## 도메인 연결 구조

`len.pe.kr`의 네임서버를 Cloudflare로 위임하고, 각 호스트명을 Cloudflare Tunnel에 연결함. N100 Windows 시작 작업은 WSL에서 `cloudflared tunnel run`을 실행하며, Tunnel은 Docker Compose 서비스의 localhost 포트로 전달함. 따라서 공유기에서 외부 `80`·`443` 포트를 직접 열지 않아도 공개 HTTPS 주소를 제공할 수 있음.

```text
브라우저
  → Cloudflare DNS / Edge
  → Cloudflare Tunnel
  → N100 WSL의 cloudflared
  → 127.0.0.1 서비스 포트
  → Docker 컨테이너
```

| 도메인 | 연결 대상 | 용도 |
|---|---:|---|
| `len.pe.kr` | `portal-web:8000` | 메인 포털 |
| `portal.len.pe.kr` | `portal-web:8000` | 기존 주소 호환 |
| `file.len.pe.kr` | `portal-web:8000` | 파일함 |
| `admin.len.pe.kr` | `portal-web:8000` | 관리자 상태 |
| `portfolio.len.pe.kr` | `portal-web:8000` | 공개 포트폴리오 |
| `news.len.pe.kr` | `crawler-worker:8001` | 나스닥 뉴스 허브 |
| `memo.len.pe.kr` | `youtube-memo:8002` | YouTube 메모 |
| `books.len.pe.kr` | `book-memo:8003` | 책 메모 |

Cloudflare Tunnel 사용 시 호스트명별 DNS는 `cloudflared tunnel route dns`로 Tunnel에 연결하며, 동일 이름의 이전 `A` 레코드는 제거함. 서비스 컨테이너는 `127.0.0.1`에만 바인드되어 외부에서 직접 접근되지 않음.

외부 `80`·`443` 포트가 열리는 환경에서는 Caddy 컨테이너를 대안으로 사용할 수 있음. 이 경우 Caddy가 Cloudflare DNS-01 인증으로 인증서를 발급하고, 동일한 도메인별 규칙을 각 Docker 서비스로 리버스 프록시함. 운영 절차는 [Cloudflare Tunnel 가이드](docs/cloudflare-tunnel.md)와 [Caddy + Cloudflare HTTPS 가이드](docs/caddy-cloudflare.md)에 분리해 정리함.

## 주요 의사결정

| 결정 | 이유 | 결과 |
|---|---|---|
| Cloudflare Tunnel을 기본 공개 경로로 선택 | 공유기 포트포워딩 없이 N100 Windows 환경에서 HTTPS 서비스를 공개하기 위함 | Cloudflare Edge에서 N100 WSL `cloudflared`로 연결하고, 호스트명별로 서비스 포트를 분기함 |
| 서비스 컨테이너를 localhost에 바인드 | 애플리케이션 포트를 인터넷에 직접 노출하지 않기 위함 | 외부 요청은 Tunnel 또는 Caddy 경계만 통과하며, 컨테이너 간 통신은 Docker 네트워크로 처리함 |
| 인증을 서비스 역할별로 분리 | 공개 포트폴리오·메모 읽기와 관리자·파일함·쓰기 권한의 위험도를 분리하기 위함 | 관리자 상태, 파일함, 포트폴리오 편집, 메모 쓰기에 서로 다른 인증 경계를 둠 |
| 병합 전 테스트·검토 게이트 적용 | AI 활용 변경도 기존 기능과 보안 계약을 깨지 않도록 하기 위함 | 기능별 RED → GREEN, 독립 재검토, 전체 회귀 테스트와 `git diff --check` 후 `main` 병합을 수행함 |

## 주요 기능

| 영역 | 구현 내용 |
|---|---|
| 포털 | 서비스 허브, 뉴스·YouTube·책 메모 통합 검색, 관리자 상태 진입 |
| 파일함 | 다중 업로드, 드래그 앤 드롭, 폴더 생성, 검색·정렬, 아이콘/목록 보기, ZIP 일괄 다운로드 |
| 관리자 상태 | CPU·메모리·디스크, 실제 host 수집 시각, 백업, Docker, 서비스 health, 보안 이벤트 |
| HomeOps | Compose 서비스 진단, 정책 기반 재시작, health 복구 검증, SQLite 이력(KST 화면 표기) |
| 뉴스 | 나스닥 관련 중요도 분류, 보관 검색, 중요 기사 텔레그램 알림 |
| 메모 | YouTube·책 데이터 관리, 읽기 공개와 쓰기 인증 분리 |
| 포트폴리오 | Markdown 기반 공개 포트폴리오와 인증된 편집 화면 |

## 보안·신뢰성

- 관리자·파일함·메모·포트폴리오의 역할별 인증 분리
- HTTP-only, SameSite, 운영 HTTPS Secure 쿠키 적용
- Origin 검증으로 교차 출처 쓰기 요청 차단
- CSP, 클릭재킹 방지, MIME 스니핑 방지, Referrer·Permissions 정책 헤더 적용
- JSON 상태 파일과 파일 잠금을 사용한 재시작·동시 요청 내성 인증 실패 제한
- 파일 경로 이탈, 위험 확장자, 업로드 용량, 덮어쓰기 방지
- 서비스별 health check, 컨테이너 자원 제한, read-only filesystem, capability drop 적용
- Docker 소켓은 `homeops-executor`에만 부여하며 Windows·WSL·Docker 엔진 재시작, 임의 셸, 다른 프로젝트 컨테이너 제어는 제외
- HomeOps Telegram은 재시작 시작·성공·실패 및 호스트 메모리 90% 3회 연속 경고/해제에만 발송하며, 토큰은 `HOMEOPS_TELEGRAM_*` 환경변수로만 관리
- 컨테이너 자동 재시작은 `unhealthy` 3회 연속 또는 CPU 85%/메모리 90% 이상과 치명 로그가 3회 연속 함께 관측된 경우로 제한
- 자동 재시작은 서비스별 10분 쿨다운과 최근 1시간 최대 2회 제한을 적용하며, 제한 도달 시 알림만 발송

## AI Engineering 적용 방식

OpenAI Codex를 개발 파트너로 사용함. 여기서 하네스 엔지니어링은 AI에게 코드 생성을 단발성으로 요청하는 대신, 구현·검토 역할을 분리하고 테스트·재현·병합 기준을 명시해 개발 루프를 운영하는 방식임.

프로젝트의 문제 정의, 우선순위, 공개 범위, 인증 정책, 운영 환경변수, 배포 여부와 최종 병합은 작성자가 판단함. Codex는 정해진 범위 안에서 구현, 테스트 작성, 코드 검토, 회귀 확인 역할을 수행함.

1. 요구사항을 기능·위험도·배포 단위로 분해함.
2. 기능별 격리 작업공간과 주제별 Git 커밋을 사용함.
3. 테스트를 먼저 작성해 RED → GREEN으로 구현함.
4. Codex 기반 구현 역할과 독립 검토 역할을 분리해 보안·회귀·설계 적합성을 검토함.
5. 중요 지적은 재현 테스트를 만든 뒤 수정하고 재검토함.
6. 작성자 승인 아래 전체 테스트와 `git diff --check`를 통과한 변경만 `main`에 병합함.

적용 사례:

- 전망성 나스닥 기사의 텔레그램 오탐을 재현 테스트로 차단함.
- 파일함 UX 개선 시 기존 업로드·다운로드·삭제 계약을 유지함.
- 공개 쓰기 경로의 인증·CSRF·CSP·동시성 제한을 독립 검토 후 보강함.
- 코드, 운영 문서, 환경변수 예시를 함께 갱신해 배포 가능한 상태를 유지함.
- HomeOps에 관측→정책→승인/제한 자동 실행→복구 검증→이력의 운영 하네스 루프를 적용함. 외부 AI API 연동은 미구현이며 규칙 기반 판단을 사용함.

## CI/CD와 검증

GitHub Actions CI는 pull request와 `main` push에서 포털·HomeOps, 시스템 상태, 크롤러·뉴스 화면, HomeOps 실행기, YouTube·책 메모, 운영·배포 설정의 서비스별 단위 테스트를 실행함. 변경 범위가 넓은 경우에는 병합 전에 추가 회귀 테스트와 `git diff --check`를 로컬에서 실행함.

`main` 브랜치에서 CI가 성공한 경우에만 별도의 `Deploy N100` workflow가 N100의 Windows self-hosted runner에서 실행됨. runner는 WSL의 배포 스크립트를 통해 원격 `main`을 갱신하고 Docker Compose 서비스를 재빌드·재기동한 뒤 서비스 health 상태를 확인함. 운영 비밀값과 데이터는 N100에만 보관하며 배포 과정에서 덮어쓰지 않음.

## 기술 스택

`Python` · `FastAPI` · `Jinja2` · `SQLite` · `Docker Compose` · `Caddy` · `Cloudflare` · `GitHub Actions` · `Windows/WSL2` · `OpenAI Codex`

## 문서

- [운영 문서 색인](docs/README.md)
- [운영 참조](docs/operations-reference.md)
- [운영 환경과 N100 설정](docs/n100-mt4-setup.md)
- [Cloudflare Tunnel](docs/cloudflare-tunnel.md)
- [Caddy + Cloudflare HTTPS](docs/caddy-cloudflare.md)
- [GitHub Actions N100 자동 배포](docs/n100-github-auto-deploy.md)
- [개발 인수인계](docs/agent-handoff.md)
- [운영 보안 QA 보고서](docs/20260702_운영보안QA_점검보고서.md)
