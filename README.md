# Personal Server

**OpenAI Codex를 활용한 하네스 엔지니어링 방식으로 설계·구현·검증한 개인 서버 프로젝트임.** 요구사항 분해, TDD, 독립 코드 검토, 보안 재검토, 증적 기반 Git 병합을 개발 루프에 적용함.

개인 생산성과 서버 운영을 위해 구축한 Docker Compose 기반 서비스 플랫폼임. 포털, 파일함, 서버 상태, 나스닥 뉴스 알림, YouTube·책 메모, 공개 포트폴리오를 하나의 시스템으로 운영함.

## 핵심 성과

- 개인용 서비스를 단일 포털과 서브도메인 기반으로 통합함.
- Investing.com RSS를 나스닥 관점으로 분류하고, 중요 기사만 텔레그램으로 즉시 알림 처리함.
- Windows 탐색기 형태의 웹 파일함에 업로드 정책, 폴더 관리, 일괄 다운로드, 키보드 조작을 구현함.
- N100 Windows host의 실제 수집 시각을 기반으로 서버·백업·컨테이너 상태를 시각화함.
- 공개 서비스의 쓰기 경로에 세션 인증, CSRF Origin 검증, CSP, 영속형 인증 실패 제한을 적용함.
- `main` push부터 N100 반영까지 GitHub Actions self-hosted runner 기반 자동 배포를 구성함.

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
  └─ Cloudflare + Caddy (HTTPS)
       ├─ portal-web      : 포털, 파일함, 관리자 상태, 포트폴리오
       ├─ crawler-worker  : Investing.com RSS, 뉴스 보관, 텔레그램 알림
       ├─ youtube-memo    : 영상·학습 메모
       └─ book-memo       : 책·목차·독서 메모

N100 Windows host
  └─ host-metrics.json → system-agent → 관리자 상태 화면

GitHub main push
  └─ GitHub Actions self-hosted runner → N100 Docker Compose 재배포
```

## 주요 기능

| 영역 | 구현 내용 |
|---|---|
| 포털 | 서비스 허브, 뉴스·YouTube·책 메모 통합 검색, 관리자 상태 진입 |
| 파일함 | 다중 업로드, 드래그 앤 드롭, 폴더 생성, 검색·정렬, 아이콘/목록 보기, ZIP 일괄 다운로드 |
| 관리자 상태 | CPU·메모리·디스크, 실제 host 수집 시각, 백업, Docker, 서비스 health, 보안 이벤트 |
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

## AI Engineering 적용 방식

OpenAI Codex를 유일한 AI 개발 파트너로 사용함. AI를 단순 코드 생성기가 아니라 개발 하네스 안의 구현·검토 역할로 사용함.

1. 요구사항을 기능·위험도·배포 단위로 분해함.
2. 기능별 격리 작업공간과 주제별 Git 커밋을 사용함.
3. 테스트를 먼저 작성해 RED → GREEN으로 구현함.
4. 구현 에이전트와 독립 검토 에이전트를 분리해 보안·회귀·설계 적합성을 검토함.
5. 중요 지적은 재현 테스트를 만든 뒤 수정하고 재검토함.
6. 전체 테스트와 `git diff --check`를 통과한 변경만 `main`에 병합함.

적용 사례:

- 전망성 나스닥 기사의 텔레그램 오탐을 재현 테스트로 차단함.
- 파일함 UX 개선 시 기존 업로드·다운로드·삭제 계약을 유지함.
- 공개 쓰기 경로의 인증·CSRF·CSP·동시성 제한을 독립 검토 후 보강함.
- 코드, 운영 문서, 환경변수 예시를 함께 갱신해 배포 가능한 상태를 유지함.

## CI/CD와 검증

`main` 브랜치에 push하면 GitHub Actions `Deploy N100` workflow가 N100의 self-hosted runner에서 실행됨. runner는 원격 `main`을 기준으로 코드를 갱신하고 서비스 컨테이너를 재빌드·재기동함. 운영 비밀값과 데이터는 N100에만 보관하며 배포 과정에서 덮어쓰지 않음.

테스트는 포털, 파일함, 뉴스 분류, 텔레그램 알림, 책·YouTube 메모, system-agent, 브라우저 동작을 서비스별로 분리해 실행함.

## 기술 스택

`Python` · `FastAPI` · `Jinja2` · `SQLite` · `Docker Compose` · `Caddy` · `Cloudflare` · `GitHub Actions` · `Windows/WSL2` · `OpenAI Codex`

## 문서

- [운영 환경과 N100 설정](docs/n100-mt4-setup.md)
- [Caddy + Cloudflare HTTPS](docs/caddy-cloudflare.md)
- [GitHub Actions N100 자동 배포](docs/n100-github-auto-deploy.md)
- [개발 인수인계](docs/agent-handoff.md)
- [운영 보안 QA 보고서](docs/20260702_운영보안QA_점검보고서.md)
