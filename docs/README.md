# 프로젝트 문서

## 현재 운영 기준

| 문서 | 내용 |
|---|---|
| [N100 VM 운영 기준](n100-vm-operations.md) | Windows·Hyper-V VM A/B, 쓰기 권한, 앱 7개와 운영 검증 |
| [Cloudflare Tunnel](cloudflare-tunnel.md) | Windows Tunnel·Caddy 진입 경로와 외부 접속 확인 |
| [운영 참조](operations-reference.md) | 현재 VM 운영 확인 경로와 이전 절차 조회 |
| [운영 로드맵](operations-roadmap.md) | 재기동·백업 복원·장애 전환의 완료 기준 |
| [변경·검증 절차](codex-work-loop.md) | 로컬 검증, 공식 CI, 운영 반영 증거 관리 |
| [작업 증적 보관 기준](agent-loop-evidence.md) | CI와 운영 결과의 보존 기준 |

## 면접·포트폴리오

| 문서 | 내용 |
|---|---|
| [N100 운영 사례 및 면접 문답](portfolio-devops.md) | 1분 소개, N100 자원 제약과 단일 쓰기, 장애 해결 과정 |
| [제출용 소개 문안](portfolio-content.md) | 프로젝트 구조, 어려웠던 점, 운영에서 확인한 결과와 한계 |
| [V1 직접 학습 계획](20261026_V1_고도화학습계획.md) | 운영 구조를 직접 설명하고 장애 확인 순서를 정리할 때 사용 |

## 이전 WSL·K3s 운영 참조

아래 절차는 VM 전환 전 구성의 기록과 도구를 확인할 때 사용함.

| 문서 | 내용 |
|---|---|
| [이전 Windows·WSL 운영](n100-mt4-setup.md) | 당시 자동 시작 및 예약 작업의 문서 |
| [K3s·모니터링·백업](../infra/k8s/README.md) | 당시 Kubernetes 운영 도구와 복원 기록 |
| [Book Memo K3s 현재 운영 기준](operations-reference.md#book-memo-k3s-현재-운영-기준) | 전환 전 Book Memo 소유권·원복 절차 |
| [뉴스 수집 K3s 현재 운영 기준](operations-reference.md#뉴스-수집-k3s-현재-운영-기준) | 전환 전 뉴스 수집기 소유권·원복 절차 |
| [복구 훈련 절차](recovery-drill.md) | 당시 Kubernetes 환경에서 실행한 격리 복구 훈련 |
