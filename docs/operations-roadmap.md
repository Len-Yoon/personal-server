# 현재 운영 로드맵

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | 현재 운영 로드맵 |
| 기준일 | 2026-09-15 |
| 기준 자료 | 저장소 운영 문서·검증 도구·문서 계약 테스트 |
| 목적 | 완료 항목과 후속 운영 개선을 분리해 관리함 |
| 비고 | 비밀값·운영 데이터·실행 자격 증명은 기록하지 않음 |

## 핵심 요약

현재 운영은 공개 경로 감시, K3s 상태·알림, Portal PVC 백업 검증, 격리된 Pod 복구 실습, 월간 SRE 통합 점검 자동화, P3 공급망 보안 검증을 제공함. 정기 실행 경계는 세 가지로 고정함: 공개 상태는 외부에서 약 5분마다 감시하고, 백업·복원 검증은 매일 실행하며, 내부 통합 점검은 매월 1일의 단일 CronJob으로 실행함. 코드 변경 검증은 이 정기 일정과 별도로 변경 시점에 수행함. 이 문서는 완료된 운영 기준과 향후 거버넌스 항목을 분리해 기록하며, 새 외부 감시 서비스나 승인 없는 자동 실행을 추가하지 않음.

## 현재 완료 기준

| 영역 | 현재 기준 | 확인 문서·도구 | 상태 |
|---|---|---|---|
| 공개 상태 감시 | GitHub Actions가 외부에서 네 공개 health를 약 5분 간격으로 확인하고 장애·복구 전환을 알림 | [공개 상태 Telegram 알림](public-uptime-monitor.md) | 완료 |
| K3s 상태·알림 | Prometheus·Alertmanager·SRE Telegram relay 경계를 점검함 | [K3s 운영](../infra/k8s/README.md), `sre-telegram-verify.sh` | 완료 |
| Portal 백업 검증 | Portal PVC 백업·복원 검증을 `--check` 읽기 점검과 별도 운영 실행으로 구분함 | [K3s 운영](../infra/k8s/README.md), `portal-pvc-backup-verify.sh` | 완료 |
| Pod 자동복구 실습 | production과 분리된 임시 namespace에서 liveness 실패와 Ready 복구를 확인함 | `sre-pod-recovery-lab.sh` | 완료 |
| 변경 검증 | 문서·설정 변경 전에 범위와 관련 검사를 확인함 | [Codex 작업 완료 루프](codex-work-loop.md) | 완료 |
| P3 공급망 보안 | GitHub Actions 외부 action을 full SHA로 고정하고, Caddy를 제외한 관리 대상 Python Docker base image 8개를 digest로 고정함. Trivy filesystem/config scan은 report-only로 실행하며 CI 계약 테스트로 검증함 | `.github/workflows/trivy-security.yml`, `tests/test_supply_chain_security_workflow.py` | 완료 |
| Cloudflare Tunnel 구성 대조 | N100 Tunnel 서비스 상태와 9개 ingress를 읽기 전용으로 대조함. Portal은 Caddy loopback, 뉴스·YouTube 메모·책 메모는 Compose 직접 ingress, 차량 callback은 비공개 upstream으로 문서화함 | [Cloudflare Tunnel](cloudflare-tunnel.md) | 완료 |
| 월간 SRE 통합 점검 운영 검증 | 활성화 전 첫 수동 Job·기존 Telegram relay·외부 health 검증을 통과했으며, 매월 1일에 실행하는 유일한 내부 정기 점검으로 Portal·K3s·백업 증적·격리 복구 훈련을 확인함. 기존 분기·validation CronJob은 suspended 상태로 보존함 | [K3s 운영](../infra/k8s/README.md), `quarterly-sre-audit-automation.sh` | 완료 |
| 백업 CronJob 실제 복구 검증 | 백업·복원 검증과 중단 시 Portal replica 복구 결과를 확인함 | [K3s 운영](../infra/k8s/README.md), `portal-pvc-backup-verify.sh` | 완료 |
| 공개 감시 범위 검토 | `portal`, `news`, `youtube_memo`, `book_memo` 공개 health 대상과 장애·복구 전환 기준을 문서와 대조함 | [공개 상태 Telegram 알림](public-uptime-monitor.md) | 완료 |
| 작업 증적 정리 | 변경 경로·CI·운영 적용 결과와 토큰 측정 미수집 상태를 재현 가능한 기준으로 기록함 | [작업 루프 증거 운영](agent-loop-evidence.md) | 완료 |
| 운영 문서 정기 검토 | 월간 SRE 설치 상태·서비스 경계·복구 절차를 실제 N100 결과와 대조하고 확인 필요 사항을 갱신함 | [K3s 운영](../infra/k8s/README.md), [복구 훈련](recovery-drill.md) | 완료 |
| Telegram relay 시험 | 분기 SRE 점검 결과가 기존 SRE Telegram relay로 전달되는 경로를 확인함 | [K3s 운영](../infra/k8s/README.md), `sre-telegram-relay` | 완료 |

## 향후 개선 항목

| 우선순위 | 항목 | 실행 기준 | 완료 조건 |
|---|---|---|---|
| P2 | 감시 경로 식별·알림 보강 | 완료된 공개 감시 실행의 실행 실패·Telegram 전달 실패를 별도 Issue·Telegram 전환으로 관리함 | 저장소 계약 테스트·독립 검토 통과 후 GitHub Actions 실제 실행 확인 필요 |
| P4 | YouTube Memo 선행 K3s 이전 | YouTube Memo만 첫 이전 대상으로 설계·검증하고 Book Memo는 안정화 뒤 별도 범위로 진행함 | 격리 검증·rollback 기준·사용자 승인 적용을 모두 충족함 |

## 검토 결과

- 완료된 과거 계획은 현재 운영 문서의 실행 항목으로 취급하지 않음.
- 공개 외부 감시는 기존 GitHub Actions를 약 5분 간격으로 유지하며, 월간 내부 점검에 통합하거나 대체하지 않음.
- Portal PVC 백업·복원 검증은 매일 실행하며, 월간 감사는 그 증적을 읽기 전용으로 확인할 뿐 백업 실행기를 대체하지 않음.
- 내부 정기 점검은 `monthly-sre-audit` 하나만 활성화함. 보존된 분기·validation CronJob은 자동 실행하지 않음.
- Portal, K3s, Caddy, Secret, PVC와 운영 데이터의 변경은 이 로드맵의 범위가 아님.
- 복구 훈련은 [복구 훈련 절차](recovery-drill.md)의 격리·읽기 점검 범위에서만 수행함.
- 월간 SRE 통합 점검은 실제 Portal·Caddy·Cloudflare Tunnel·Compose 서비스를 중단하거나 재시작하지 않으며, 결과를 `monitoring/sre-telegram-quarterly-audit-status` ConfigMap에서 기존 relay로 전달함.

## 확인 필요 사항

- 실제 모델 토큰 측정 기록은 수집하지 않았으므로, 토큰 절감률은 확인 필요함.

## 후속 조치

1. 매월 1일 월간 CronJob 실행 뒤 status ConfigMap과 Telegram relay 결과를 확인함. 수동 실행은 긴급·추가 점검이 필요한 경우에만 사용함.
2. 매일 백업 결과와 약 5분 간격 공개 감시의 장애·복구 전환은 월간 점검을 기다리지 않고 확인함.
3. 실패 또는 중단 시 운영 데이터를 변경하지 않고 원인을 기록함.
4. 문서와 실제 도구의 명령·성공 기준이 달라지면 문서 계약 테스트를 먼저 갱신함.
