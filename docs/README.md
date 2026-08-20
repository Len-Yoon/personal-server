# 문서 색인

이 디렉터리는 공개 포트폴리오에 활용할 프로젝트 요약, 현재 운영 기준, 과거 설계·점검 이력을 분리해 보관함. 서비스·환경변수·배포 명령은 코드와 Compose 설정을 기준으로 확인하며, 운영 작업에는 **현재 운영 문서**만 사용함.

> 마지막 정합성 점검: 2026-08-20. 공개 README, Compose 설정, GitHub Actions workflow, 환경변수 예시와 대조함.

## 포트폴리오 참고 자료

| 문서 | 용도 | 비고 |
|---|---|---|
| [프로젝트 README](../README.md) | 채용·이력서용 프로젝트 요약 | 구현 범위, 의사결정, 검증 근거 중심 |
| [공개 포트폴리오 Markdown 초안](portfolio-content.md) | `portfolio.len.pe.kr`에 게시할 원문 | 관리자 편집 화면에 복사해 사용 가능 |

## 현재 운영 문서

| 문서 | 용도 | 기준 구성 |
|---|---|---|
| [운영 참조](operations-reference.md) | 서비스·도메인·환경변수·점검 명령 요약 | Compose, `.env.example`, workflow |
| [N100 운영 환경](n100-mt4-setup.md) | Windows, WSL2, Docker, host metrics 운영 | N100 Windows + Ubuntu-24.04 |
| [GitHub 자동 배포](n100-github-auto-deploy.md) | self-hosted runner와 배포 장애 대응 | `Deploy N100` workflow |
| [Cloudflare Tunnel](cloudflare-tunnel.md) | 포트포워딩 없이 공개 도메인을 연결하는 방법 | `cloudflared` ingress |
| [Caddy + Cloudflare](caddy-cloudflare.md) | 외부 `80`·`443`을 직접 여는 대체 HTTPS 구성 | Caddy DNS-01 |
| [작업 인수인계](agent-handoff.md) | 후속 개발·운영 작업자의 코드 위치와 규칙 | 현재 저장소 구조 |

## 과거 기록

- [운영보안 QA 보고서](20260702_운영보안QA_점검보고서.md): 2026-07-02 기준의 점검 결과임. 현재 운영값은 현재 운영 문서를 우선함.
- [`superpowers/`](superpowers/README.md): 기능 설계·구현 계획의 이력 보관함. 완료·변경 전 계획을 포함하므로 현재 기능 설명이나 운영 절차의 근거로 사용하지 않음.

## 문서 갱신 원칙

- 공개 README와 포트폴리오 원문에는 코드·테스트·설정으로 확인 가능한 사실만 기록함.
- 서비스명·포트·도메인·workflow·환경변수 기본값은 코드와 설정 파일을 기준으로 갱신함.
- 비밀값, 토큰, 비밀번호, 개인 경로는 문서에 기록하지 않음.
- 과거 문서는 당시 판단을 보존하고, 최신 기준으로 오인될 수 있는 경우에만 기준 시점을 표시함.
