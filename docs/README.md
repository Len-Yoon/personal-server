# 운영 문서 색인

이 디렉터리는 개인 서버의 현재 운영 기준과 과거 설계·점검 기록을 함께 보관함. 운영 작업은 먼저 **현재 운영 문서**를 기준으로 판단하고, 과거 문서는 변경 배경을 확인하는 용도로만 사용함.

## 현재 운영 문서

| 문서 | 용도 | 기준 구성 |
|---|---|---|
| [운영 참조](operations-reference.md) | 서비스·도메인·환경변수·점검 명령 요약 | Compose, `.env.example`, workflow |
| [N100 운영 환경](n100-mt4-setup.md) | Windows, WSL2, Docker, host metrics 운영 | N100 Windows + Ubuntu-24.04 |
| [GitHub 자동 배포](n100-github-auto-deploy.md) | self-hosted runner와 배포 장애 대응 | `Deploy N100` workflow |
| [Cloudflare Tunnel](cloudflare-tunnel.md) | 포트포워딩 없이 공개 도메인을 연결하는 방법 | `cloudflared` ingress |
| [Caddy + Cloudflare](caddy-cloudflare.md) | 외부 `80`·`443`을 직접 여는 대체 HTTPS 구성 | Caddy DNS-01 |
| [작업 인수인계](agent-handoff.md) | 후속 개발·운영 작업자가 확인할 코드 위치와 규칙 | 현재 저장소 구조 |

## 과거 기록

- [운영보안 QA 보고서](20260702_운영보안QA_점검보고서.md): 2026-07-02 기준의 점검 결과임. 현재 운영값은 위 현재 운영 문서를 우선함.
- [`superpowers/specs/`](superpowers/specs/): 기능 설계 기록임.
- [`superpowers/plans/`](superpowers/plans/): 기능 구현 계획과 검토 이력임.

## 문서 갱신 원칙

- 서비스명·포트·도메인·workflow·환경변수 기본값은 코드와 설정 파일을 기준으로 갱신함.
- 비밀값, 토큰, 비밀번호, 개인 경로는 문서에 기록하지 않음.
- 과거 문서는 당시 판단을 보존하고, 최신 기준으로 오인될 수 있는 경우에만 기준 시점을 표시함.
