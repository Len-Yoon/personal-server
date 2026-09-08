# 운영 문서 색인

이 디렉터리는 현재 운영에 필요한 문서만 보관함. 서비스명·도메인·운영 절차는 아래 문서를 기준으로 하며, 비밀값은 어떤 문서에도 기록하지 않음.

## 현재 운영 기준

| 문서 | 사용할 때 |
|---|---|
| [현재 운영 로드맵](operations-roadmap.md) | 완료 기준과 향후 운영 개선 항목 확인 |
| [안전한 복구 훈련](recovery-drill.md) | 월간 백업·알림·격리 Pod 복구 점검 |
| [운영 참조](operations-reference.md) | 전체 서비스 구조, 공개 경로, 일상 상태 확인 |
| [N100 운영 환경](n100-mt4-setup.md) | Windows·WSL2 자동 시작, 제한형 자동복구, 재부팅 뒤 확인, 자원 점검 |
| [K3s·모니터링·백업](../infra/k8s/README.md) | Portal K3s, Grafana, Telegram SRE relay, PVC 백업 |
| [뉴스 수집 관측성](operations-reference.md#뉴스-수집-관측성) | crawler 수집 상태·인증 metrics·NewsCollectionStale 운영 기준 |
| [Cloudflare Tunnel](cloudflare-tunnel.md) | 현재 공개 경로와 터널 장애 대응 |
| [공개 상태 Telegram 알림](public-uptime-monitor.md) | 약 5분 외부 점검과 장애·복구 알림 조건 |
| [N100 안전 자동 배포](n100-github-auto-deploy.md) | 허용 서비스의 GitHub Actions 배포 |
| [N100 원격 개발](n100-remote-development.md) | Mac에서 N100 WSL 작업 환경 사용 |
| [작업 인수인계](agent-handoff.md) | 저장소 구조와 작업 경계 확인 |
| [Codex 작업 완료 루프](codex-work-loop.md) | 변경·검증·PR 절차 |
| [작업 루프 증거 운영](agent-loop-evidence.md) | CI 결과와 증적 보관 기준 |

## 문서 갱신 원칙

- README와 운영 문서는 코드·설정·실행 검증으로 확인된 현재 사실만 기록함.
- 이전 설계안, 완료된 구현 계획, 폐기된 전환 초안은 운영 문서에 보관하지 않음.
- Portal, K3s, Caddy, Secret, PVC, 운영 데이터 변경 절차는 자동 배포 문서와 분리함.
- 비밀번호, 토큰, chat ID, Secret 값, 개인 경로는 기록하지 않음.
