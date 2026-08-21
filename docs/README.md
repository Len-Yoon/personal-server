# 문서 색인

이 디렉터리는 현재 운영 기준, 참고 자료, 과거 설계·점검 이력을 분리해 보관함. 서비스·환경변수·배포 명령은 코드와 Compose 설정을 기준으로 확인하며, 운영 작업에는 **현재 운영 기준**만 사용함.

> 마지막 정합성 점검: 2026-08-22. 현재 운영 문서의 기준·참조 관계, Compose 설정, GitHub Actions workflow와 대조함.

## 현재 운영 기준

권장 열람 순서는 [운영 참조](operations-reference.md) → N100 또는 배포 문서 → 공개 HTTPS 방식 → Codex 작업 기준임.

| 문서 | 사용할 때 | 기준 구성 |
|---|---|---|
| [운영 참조](operations-reference.md) | 서비스·도메인·환경변수·일상 점검을 빠르게 확인할 때 | Compose, `.env.example`, workflow |
| [N100 운영 환경](n100-mt4-setup.md) | Windows·WSL2·Docker 최초 구성과 일상 운영을 확인할 때 | N100 Windows + Ubuntu-24.04 |
| [GitHub 자동 배포](n100-github-auto-deploy.md) | self-hosted runner, 배포, 배포 장애를 확인할 때 | `Deploy N100` workflow |
| [Cloudflare Tunnel](cloudflare-tunnel.md) | 포트포워딩 없이 공개 HTTPS를 구성할 때 | `cloudflared` ingress |
| [Caddy + Cloudflare](caddy-cloudflare.md) | 외부 `80`·`443`을 직접 공개할 때 | Caddy DNS-01 |
| [작업 인수인계](agent-handoff.md) | 코드 위치와 기능 계약을 빠르게 파악할 때 | 현재 저장소 구조 |
| [Codex 작업 완료 루프](codex-work-loop.md) | 변경·검증·브랜치 정리 절차를 따를 때 | 프로젝트 작업 규칙 |
| [작업 루프 증거 운영](agent-loop-evidence.md) | CI artifact 확인과 장기 증거 보존을 처리할 때 | GitHub Actions artifact |

Tunnel과 Caddy는 대체 공개 경로이므로 동시에 운영하지 않음. 실제 공개 경로와 포트는 운영 참조를 기준으로 확인함.

## 참고 자료

| 문서 | 용도 | 비고 |
|---|---|---|
| [프로젝트 README](../README.md) | 채용·이력서용 프로젝트 요약 | 구현 범위, 의사결정, 검증 근거 중심 |
| [공개 포트폴리오 Markdown 초안](portfolio-content.md) | 공개 포트폴리오에 게시할 원문 | 관리자 편집 화면에 복사해 사용 가능 |

## 과거 이력

- [운영보안 QA 보고서](20260702_운영보안QA_점검보고서.md): 2026-07-02 기준의 점검 결과임. 현재 운영값은 현재 운영 문서를 우선함.
- [`superpowers/`](superpowers/README.md): 기능 설계·구현 계획의 이력 보관함. 완료·변경 전 계획을 포함하므로 현재 기능 설명이나 운영 절차의 근거로 사용하지 않음.

## 문서 갱신 원칙

- 공개 README와 포트폴리오 원문에는 코드·테스트·설정으로 확인 가능한 사실만 기록함.
- 서비스명·포트·도메인·workflow·환경변수 기본값은 코드와 설정 파일을 기준으로 갱신함.
- 비밀값, 토큰, 비밀번호, 개인 경로는 문서에 기록하지 않음.
- 과거 문서는 당시 판단을 보존하고, 최신 기준으로 오인될 수 있는 경우에만 기준 시점을 표시함.
