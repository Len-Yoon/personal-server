# 현재 운영 로드맵

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | 현재 운영 로드맵 |
| 기준일 | 2026-10-09 백업 보완 결과 갱신. 기존 운영 검증 시점은 각 증적을 따름 |
| 기준 자료 | 저장소 운영 문서·검증 도구·문서 계약 테스트 |
| 목적 | 완료 항목과 후속 운영 개선을 분리해 관리함 |
| 비고 | 비밀값·운영 데이터·실행 자격 증명은 기록하지 않음 |

## 핵심 요약

현재 운영은 공개 경로 감시, K3s 상태·알림, Portal PVC 백업 검증, 격리된 Pod 복구 실습, 월간 SRE 통합 점검 자동화, P3 공급망 보안 검증을 제공함. 정기 실행은 공개 상태의 약 5분 간격 외부 감시, 매일 백업·복원 검증, 일일 SLO 증적 수집, 매월 1일의 단일 CronJob 통합 점검, GitHub의 주간 Trivy 검증으로 구분함. 일일 SLO 수집은 2026-09-22 운영 적용 뒤 2026-09-23 예약 실행 성공을 확인함. Trivy 주간 일정은 저장소 설정 기준이며 첫 예약 실행 결과는 확인 필요함. 코드 변경 검증은 이 정기 일정과 별도로 변경 시점에 수행함. 이 문서는 완료된 운영 기준과 향후 거버넌스 항목을 분리해 기록함.

현재 활성 스케줄러와 마지막 성공 시각은 [운영 상태 확인 기록](operations-reference.md#운영-상태-확인-기록)을 기준으로 함. 아래 완료 항목은 구현·과거 검증의 기록이며 모든 서비스의 최신 live 상태를 이번에 재검증했다는 의미는 아님.

## 현재 완료 기준

| 영역 | 현재 기준 | 확인 문서·도구 | 상태 |
|---|---|---|---|
| 공개 상태 감시 | GitHub Actions가 외부에서 네 공개 health를 약 5분 간격으로 확인하고 장애·복구 전환을 알림 | [공개 상태 Telegram 알림](public-uptime-monitor.md) | 완료 |
| K3s 상태·알림 | Prometheus·Alertmanager·SRE Telegram relay 경계를 점검함 | [K3s 운영](../infra/k8s/README.md), `sre-telegram-verify.sh` | 완료 |
| Portal 백업 검증 | 02:30 단일 순차 timer·중지된 개별 Cron 템플릿으로 운영함. 10월 8일 실패 수정·새 Production 인증의 신규 업로드/격리 복원·소비자 갱신을 확인함 | [최신 복구 기록](reviews/20261009_Portal백업_증적만료보완.md), [현재 실행 주체](operations-reference.md#운영-상태-확인-기록) | 10월 9일 네 자동 Job 성공, Portal 재사용 만료 보완·배포·20:40 신규 백업 검증 이후 별도 복원·소비자 확인 완료. 보완 이후 다음 자동 실행 확인 필요 |
| 일일 SLO 증적 | freshness 다중 시계열 집계 보완을 운영 적용하고, 수동 검증 뒤 CronJob 자동 실행을 활성화함 | [뉴스·SLO 운영 적용 검증 결과](reviews/20260922_뉴스_SLO_운영적용_검증결과.md), `slo-daily-evidence.py` | 완료 |
| Pod 자동복구 실습 | production과 분리된 임시 namespace에서 liveness 실패와 Ready 복구를 확인함 | `sre-pod-recovery-lab.sh` | 완료 |
| 변경 검증 | 문서·설정 변경 전에 범위와 관련 검사를 확인함 | [Codex 작업 완료 루프](codex-work-loop.md) | 완료 |
| P3 공급망 보안 | GitHub Actions 외부 action을 full SHA로 고정하고, Caddy를 제외한 관리 대상 Python Docker base image 8개를 digest로 고정함. Trivy filesystem/config scan은 HIGH·CRITICAL 결과를 exit-code 1로 차단하며 CI 계약 테스트로 검증함 | `.github/workflows/trivy-security.yml`, `tests/test_supply_chain_security_workflow.py` | 완료 |
| 주간 공급망 재검증 | 같은 Trivy filesystem/config 검사를 매주 월요일 11:17 KST와 수동 실행에 재사용함. 예약 실행은 GitHub 사정에 따라 지연·누락될 수 있으므로 실제 run과 결과를 별도로 확인함. N100 로컬 이미지는 이 검사 범위에 포함되지 않음 | `.github/workflows/trivy-security.yml` | 첫 예약 실행 확인 필요 |
| Cloudflare Tunnel 구성 대조 | N100 Tunnel 서비스와 9개 ingress를 대조함. Portal·Crawler Worker·Book Memo·YouTube Memo는 Caddy loopback, 차량 callback은 비공개 upstream으로 문서화함 | [Cloudflare Tunnel](cloudflare-tunnel.md) | 완료 |
| Crawler Worker K3s 이전 | Docker writer 중지, 데이터 무결성 대조 후 K3s writer 기동과 Caddy·Tunnel 경로 전환을 완료함. Docker와 K3s의 동시 production write를 허용하지 않음 | [운영 참조](operations-reference.md#뉴스-수집-k3s-현재-운영-기준) | 완료 |
| YouTube Memo K3s 이전 | Docker writer 중지, 데이터 무결성 대조 후 K3s writer 기동과 Caddy·Tunnel 경로 전환을 완료함. Docker와 K3s의 동시 production write를 허용하지 않음 | [운영 참조](operations-reference.md#youtube-memo-k3s-현재-운영-기준) | 완료 |
| 월간 SRE 통합 점검 운영 검증 | 활성화 전 첫 수동 Job·기존 Telegram relay·외부 health 검증을 통과했으며, 매월 1일에 실행하는 유일한 내부 정기 점검으로 Portal·K3s·백업 증적·격리 복구 훈련을 확인함. 기존 분기·validation CronJob은 suspended 상태로 보존함 | [K3s 운영](../infra/k8s/README.md), `quarterly-sre-audit-automation.sh` | 완료 |
| 백업 CronJob 실제 복구 검증 | 백업·복원 검증과 중단 시 Portal replica 복구 결과를 확인함 | [K3s 운영](../infra/k8s/README.md), `portal-pvc-backup-verify.sh` | 완료 |
| 공개 감시 범위 검토 | `portal`, `news`, `youtube_memo`, `book_memo` 공개 health 대상과 장애·복구 전환 기준을 문서와 대조함 | [공개 상태 Telegram 알림](public-uptime-monitor.md) | 완료 |
| 작업 증적 정리 | 변경 경로·CI·운영 적용 결과와 토큰 측정 미수집 상태를 재현 가능한 기준으로 기록함 | [작업 루프 증거 운영](agent-loop-evidence.md) | 완료 |
| 운영 문서 정기 검토 | 월간 SRE 설치 상태·서비스 경계·복구 절차를 실제 N100 결과와 대조하고 확인 필요 사항을 갱신함 | [K3s 운영](../infra/k8s/README.md), [복구 훈련](recovery-drill.md) | 완료 |
| Telegram relay 시험 | 분기 SRE 점검 결과가 기존 SRE Telegram relay로 전달되는 경로를 확인함 | [K3s 운영](../infra/k8s/README.md), `sre-telegram-relay` | 완료 |

## 향후 개선 항목

### 다음 백업·인증 확인

| 우선순위 | 항목 | 완료 조건 | 비고 |
|---|---|---|---|
| P1 | Portal 수정 후 정기 백업 | 다음 소유 Job 종료·신규 업로드·복원 증적·atomic export·소비자 fresh 확인 | 수동 성공을 정기 실행 성공으로 대체하지 않음 |
| P2 | 다른 세 백업 인증 | Book·YouTube·Crawler 각 인증 출처·유효성 확인 및 필요 조치의 검증 | 이 문서 갱신으로 자격 증명 변경을 실행하지 않음 |
| P2 | 전체 서비스 잔여 검증 | [개발 계획](20260921_프로젝트보완_개발계획.md)의 미검증 항목과 실제 증적 대조 | 기능 구현·저장소 반영·운영 성공을 구분함 |

### Loki·Ansible 후속 실습 상태

최초 `f66a42a` ([PR #331](https://github.com/Len-Yoon/personal-server/pull/331) 병합) 실습과 후속 `6a17f5a` 운영 결과를 함께 기록함. 저장소 검증 완료와 N100 실동작 검증 결과를 구분함. 면접 설명은 [DevOps 포트폴리오](portfolio-devops.md)를 참조함.

| 항목 | 현재 상태 | 남은 조건 |
|---|---|---|
| Loki·Alloy·Grafana 실습 | 최초 실습 후 10월 5일 rollback·재설치·같은 PVC/이전 로그 보존·샘플 조회 완료 | 최초 조회 실패 원인·retention·hard quota 확인 필요 |
| Ansible localhost 실습 | 최초 실습과 후속 실제 drift 감지·원복·멱등성 재훈련 완료 | 최초 실패·수동 원상복구 이력 유지, 최종 `6a17f5a`의 6개 상태 PASS·changed=0 |
| 저장소 통합 | 최초 PR #331 `f66a42a`, 후속 PR #333·#334·#335와 최종 `6a17f5a` 반영 완료 | 각 필수 CI·Trivy·독립 검토 통과. 정책상 자동배포 제외와 승인된 수동 적용을 구분함 |
| N100 실습 적용 | 최초 실습과 후속 정확한 SHA 동기화·최종 재훈련 및 보호 검증 완료 | [최초 결과](reviews/20261004_Loki_Ansible_실습_N100운영검증결과.md), [10월 5일 후속 결과](reviews/20261005_DevOps실습_운영적용결과.md). 최초 실패와 최종 성공을 분리함 |

| 우선순위 | 항목 | 실행 기준 | 완료 조건 |
|---|---|---|---|
| P2 | 감시 경로 식별·알림 보강 | 완료된 공개 감시 실행의 실행 실패·Telegram 전달 실패를 별도 Issue·Telegram 전환으로 관리함 | 저장소 계약 테스트·독립 검토 통과 후 GitHub Actions 실제 실행 확인 필요 |

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

2026-10-05 후속 실습은 [고도화 검증 결과](reviews/20261005_DevOps실습고도화_검증결과.md)의 코드 준비 뒤 [실제 운영 결과](reviews/20261005_DevOps실습_운영적용결과.md)로 완료함. 격리 실패 배포·Loki 데이터 보존 rollback/재설치·Ansible 실패 후 원상복구와 최종 재훈련·SLO image-only 교체를 확인함. SLO 새 이미지의 다음 정기 collector 전체 성공은 이 실습 결과로 대체하지 않음. 추가 운영 변경은 대상·동작별 승인 범위를 따름.

1. 매월 1일 월간 CronJob 실행 뒤 status ConfigMap과 Telegram relay 결과를 확인함. 수동 실행은 긴급·추가 점검이 필요한 경우에만 사용함.
2. 매일 백업 결과와 약 5분 간격 공개 감시의 장애·복구 전환은 월간 점검을 기다리지 않고 확인함.
3. 실패 또는 중단 시 운영 데이터를 변경하지 않고 원인을 기록함.
4. 문서와 실제 도구의 명령·성공 기준이 달라지면 문서 계약 테스트를 먼저 갱신함.
