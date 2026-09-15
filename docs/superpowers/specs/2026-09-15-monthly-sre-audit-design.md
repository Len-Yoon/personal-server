# 월간 SRE 통합 점검 설계

## 목적

내부 정기 점검을 월간 단일 SRE 감사로 통합함. 일일 Portal PVC 백업 실행과 외부 공개 상태 감시는 각각의 독립 실행 경계를 유지함.

## 범위

| 구분 | 처리 |
|---|---|
| 외부 공개 상태 감시 | GitHub Actions 5분 감시를 유지함. 서버 장애 시에도 독립 확인 경로로 사용함. |
| Portal PVC 백업 | 기존 일일 CronJob만 실행기로 유지함. 통합 감사는 Secret·rclone 자격 증명에 접근하지 않고 최신 백업·복원 증적만 읽음. |
| 내부 SRE 감사 | 기존 분기 감사의 Portal·노드·백업 증적·격리 Pod 복구 검사를 월 1회 단일 CronJob으로 실행함. |
| 수동 실행 | 동일 CronJob에서 수동 Job을 생성해 정기 실행과 같은 검증·결과 기록 경로를 사용함. |

## 설계

1. 새 `monthly-sre-audit` CronJob을 월간 일정으로 추가하고, 기존 `quarterly-sre-audit` 및 validation CronJob은 `suspend: true`로 보존함. 같은 검증 세트를 두 CronJob에서 동시에 활성화하지 않음.
2. runner는 Portal 가용성, K3s 노드, 최신 백업·복원 증적, 격리된 Pod liveness 복구를 순서대로 점검하고 결과 ConfigMap에 최소 정보만 기록함.
3. Telegram relay의 기존 결과 ConfigMap 이름·필드 계약은 유지함. 첫 전환에서 relay 자격 증명·동작을 변경하지 않음.
4. 기존 수동 월간 복구 실행기는 원격 백업 명령을 직접 호출하지 않음. 대신 Kubernetes의 백업 증적 계약을 검사하는 전용 읽기 도구를 호출함.
5. 월간 통합 감사와 기존 분기 감사가 동시에 활성화되지 않도록 설치·상태·문서 계약을 갱신함.

## 안전 경계

- Portal PVC, Secret, 운영 데이터, Caddy, Tunnel ingress는 수정하지 않음.
- 백업 Secret·rclone 설정·Telegram 자격 증명은 읽거나 출력하지 않음.
- Pod 복구 실습은 `sre-recovery-lab` namespace와 허용된 Deployment·trigger ConfigMap만 사용함.
- 외부 공개 감시와 일일 백업 스케줄은 제거하거나 통합하지 않음.
- 월간 감사는 `concurrencyPolicy: Forbid`, bounded deadline, 최소 RBAC를 유지함.

## 성공 기준

- 월간 CronJob은 단 하나이며 활성화 전 수동 Job 성공을 요구함.
- 수동 월간 실행기와 CronJob 모두 최신 백업 증적을 Secret 없이 확인함.
- 실패 시 실패 단계와 정리 상태만 기록하며 Secret·명령 출력은 증적에 남기지 않음.
- 기존 외부 공개 감시와 일일 백업 CronJob 계약은 변경되지 않음.
