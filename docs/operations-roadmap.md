# 현재 운영 로드맵

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | 현재 운영 로드맵 |
| 기준일 | 2026-09-10 |
| 기준 자료 | 저장소 운영 문서·검증 도구·문서 계약 테스트 |
| 목적 | 완료 항목과 후속 운영 개선을 분리해 관리함 |
| 비고 | 비밀값·운영 데이터·실행 자격 증명은 기록하지 않음 |

## 핵심 요약

현재 운영은 공개 경로 감시, K3s 상태·알림, Portal PVC 백업 검증, 격리된 Pod 복구 실습, P3 공급망 보안 검증을 제공함. 이 문서는 완료된 운영 기준과 향후 개선 항목을 분리해 기록하며, 새 외부 감시 서비스나 자동 실행을 추가하지 않음.

## 현재 완료 기준

| 영역 | 현재 기준 | 확인 문서·도구 | 상태 |
|---|---|---|---|
| 공개 상태 감시 | GitHub Actions가 외부에서 `https://len.pe.kr/health`를 약 5분 간격으로 확인하고 장애·복구 전환을 알림 | [공개 상태 Telegram 알림](public-uptime-monitor.md) | 완료 |
| K3s 상태·알림 | Prometheus·Alertmanager·SRE Telegram relay 경계를 점검함 | [K3s 운영](../infra/k8s/README.md), `sre-telegram-verify.sh` | 완료 |
| Portal 백업 검증 | Portal PVC 백업·복원 검증을 `--check` 읽기 점검과 별도 운영 실행으로 구분함 | [K3s 운영](../infra/k8s/README.md), `portal-pvc-backup-verify.sh` | 완료 |
| Pod 자동복구 실습 | production과 분리된 임시 namespace에서 liveness 실패와 Ready 복구를 확인함 | `sre-pod-recovery-lab.sh` | 완료 |
| 변경 검증 | 문서·설정 변경 전에 범위와 관련 검사를 확인함 | [Codex 작업 완료 루프](codex-work-loop.md) | 완료 |
| P3 공급망 보안 | GitHub Actions 외부 action을 full SHA로 고정하고, Caddy를 제외한 관리 대상 Python Docker base image 8개를 digest로 고정함. Trivy filesystem/config scan은 report-only로 실행하며 CI 계약 테스트로 검증함 | `.github/workflows/trivy-security.yml`, `tests/test_supply_chain_security_workflow.py` | 완료 |

## 향후 개선 항목

| 우선순위 | 항목 | 실행 기준 | 완료 조건 |
|---|---|---|---|
| P1 | 월간 복구 훈련 정례화 | `monthly-recovery-drill.sh`를 월 1회 수동 실행하고 결과 JSON을 운영 기록에 남김 | 백업 점검·알림 점검·격리 Pod 복구 결과와 미해결 이슈가 비밀값 없이 원자 기록됨 |
| P1 | 공개 감시 범위 검토 | 기존 GitHub Actions workflow와 실제 공개 health endpoint의 일치 여부를 검토함 | 감시 대상·간격·알림 전환 기준이 문서와 일치함 |
| P2 | 작업 증적 정리 | 변경 경로, 검사 결과, 토큰 측정 기록을 로컬 증적으로 관리함 | 검증 결과가 작업별로 재현 가능함 |
| P2 | 운영 문서 정기 검토 | 분기별로 서비스 경로·운영 경계·복구 절차를 실제 구성과 대조함 | 폐기된 절차와 확인 필요 항목이 분리됨 |
| P3 | Dependabot 자동 PR 구성(완료) | GitHub Actions 루트 및 book-memo, car-care-worker, crawler-worker, homeops-executor, portal-web, sre-telegram-relay, system-agent, youtube-memo Docker 디렉터리를 매주 월요일 점검하여 자동 PR만 생성함. Caddy는 제외함 | 자동 병합·자동 배포·Secret 사용 없음 |

## 검토 결과

- 완료된 과거 계획은 현재 운영 문서의 실행 항목으로 취급하지 않음.
- 공개 외부 감시는 기존 GitHub Actions를 유지하며 별도 감시 서비스를 추가하지 않음.
- Portal, K3s, Caddy, Secret, PVC와 운영 데이터의 변경은 이 로드맵의 범위가 아님.
- 복구 훈련은 [복구 훈련 절차](recovery-drill.md)의 격리·읽기 점검 범위에서만 수행함.

## 확인 필요 사항

- 월간 훈련의 실제 실행일과 결과 보관 위치는 운영자가 정해야 함.
- 공개 감시 대상에 `news`, `memo`, `books` health endpoint를 추가할지는 별도 검토가 필요함.

## 후속 조치

1. 월 1회 [복구 훈련 절차](recovery-drill.md)를 실행함.
2. 실패 또는 중단 시 운영 데이터를 변경하지 않고 원인을 기록함.
3. 문서와 실제 도구의 명령·성공 기준이 달라지면 문서 계약 테스트를 먼저 갱신함.
