# 안전한 복구 훈련 절차

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | 월간 안전 복구 훈련 절차 |
| 실행 주기 | 월 1회 수동 실행 |
| 기준 자료 | `infra/k8s/tools`의 검증·실습 도구 |
| 목적 | 백업 사전조건, SRE 알림 경계, 격리 Pod 자동복구를 확인함 |
| 비고 | production Portal·공개 경로·PVC·운영 데이터는 변경하지 않음 |

## 핵심 요약

본 문서는 승인된 통제 훈련 절차임. 이번 변경에서는 실제 Tunnel 중지·자동복구·재부팅을 수행하지 않음. 실제 Tunnel-only 훈련은 별도 사용자 승인 뒤에만 실행하며, 다음 순서와 외부 health·Telegram 결과를 증적으로 남김.

## Tunnel 장애 모의 순서

`Tunnel만` 중지 → N100 Tunnel 장애 Telegram 확인 → 자동복구 대기 → 외부 health와 N100 복구 Telegram 확인 → GitHub monitor의 독립 외부 점검 결과 확인 → 실패 시 사용자 서비스 수동 시작 또는 재시작 순서로 실행함. 실제 실행 전에는 별도 사용자 승인이 필요함. GitHub monitor 알림은 N100 직접 알림과 중복될 수 있음. 서비스가 inactive면 `systemctl --user start cloudflared-personal-server.service`, active지만 연결 프로세스가 없으면 `systemctl --user restart cloudflared-personal-server.service`를 사용함.

## 사전 조건

- N100 WSL에서 저장소 루트로 이동함.
- 운영자 권한과 K3s가 정상적으로 접근되는지 확인함.
- 실행 전 장애나 배포가 진행 중이면 훈련을 시작하지 않음.
- 출력에 Secret 값, token, chat ID, 개인 경로가 포함되지 않는지 확인함.
- N100 직접 Telegram 자격 증명이 설정됐고 수신이 가능한 상태인지 사전에 확인함.
- N100 전원·네트워크·WSL 자체가 동작하지 않는 장애는 이 훈련의 자동복구·직접 알림 대상이 아님.

## 월간 체크리스트

### 1. Portal PVC 백업 사전조건 점검

```bash
bash infra/k8s/tools/portal-pvc-backup-verify.sh --check
```

성공 기준은 `personal-server` namespace, K3s runtime marker, PVC `Bound` 상태, 단일 Portal replica, 암호화 자격 증명 파일 접근 조건이 통과하는 것임. 이 명령은 백업 업로드나 복원 실행을 대신하지 않음.

### 2. SRE Telegram relay·Prometheus 점검

```bash
bash infra/k8s/tools/sre-telegram-verify.sh
```

성공 기준은 relay rollout, 내부 전용 ClusterIP 노출, PrometheusRule, 비확장 RBAC, relay health, Prometheus target이 모두 통과하는 것임. 명령은 Secret 값을 출력하지 않음.

### 3. 격리된 Pod 자동복구 실습

```bash
bash infra/k8s/tools/sre-pod-recovery-lab.sh --run
```

성공 기준은 임시 namespace에서 liveness 실패 뒤 `restartCount`가 증가하고 동일 실습 Pod가 `Ready` 상태로 복구되는 것임. `--run`은 성공·실패 시 실습 namespace 정리를 시도하며, 출력된 run ID가 있으면 결과 기록에 남김.

자동 정리가 확인되지 않거나 중단 후 namespace가 남은 경우에만 해당 run ID로 정리함.

```bash
bash infra/k8s/tools/sre-pod-recovery-lab.sh --cleanup <run-id>
```

정리 성공 기준은 `sre-recovery-lab-<run-id>` namespace가 더 이상 존재하지 않는 것임.

## 중단 기준과 복구 조치

아래 중단 기준은 1·2단계의 정상적인 `personal-server`·`monitoring` 읽기 점검에는 적용하지 않으며, 3단계 격리된 Pod 자동복구 실습에만 적용함.

다음 중 하나라도 발생하면 즉시 훈련을 중단함.

| 상황 | 조치 |
|---|---|
| 3단계 실습 대상 namespace가 `personal-server` 또는 `monitoring`으로 표시됨 | 명령을 중단하고 리소스를 삭제·변경하지 않은 채 운영자에게 보고함 |
| Portal deployment, PVC, Caddy, 공개 경로가 변경될 조짐이 있음 | `Ctrl+C`로 중단하고 현재 상태만 확인함. cutover·rollback 명령은 실행하지 않음 |
| `--run`이 실패하거나 중간 종료됨 | 출력된 run ID로 `--cleanup <run-id>`를 실행하고 namespace 부재를 확인함 |
| Secret·token·chat ID가 출력됨 | 출력 공유를 중단하고 값을 저장하지 않음. 관련 로그·증적은 운영자 보안 절차로 처리함 |
| 점검 중 실제 운영 장애가 발생함 | 훈련을 중단하고 기존 장애 대응 절차로 전환함 |

중단 후에는 Portal replica를 임의로 조정하거나 PVC를 삭제·복원하지 않음. 운영 데이터 복구가 필요한 경우 별도 승인된 운영 절차를 사용함.

## 결과 기록

각 단계에 대해 아래 항목만 기록함.

| 항목 | 기록값 |
|---|---|
| 실행 시각 | UTC ISO 8601 내부 기록 |
| 백업 점검 | PASS 또는 FAIL |
| 알림 점검 | PASS 또는 FAIL |
| Pod 복구 실습 | PASS 또는 FAIL 및 run ID |
| 중단·예외 | 사실과 후속 조치만 기록함 |

토큰, 비밀번호, Secret 값, chat ID, 운영 데이터 내용은 결과 기록에 포함하지 않음.

## 확인 필요 사항

- 실제 결과 보관 위치와 월간 실행 담당자는 운영자가 지정해야 함.
- 이 절차는 자동 실행을 추가하지 않으며, scheduler·timer·CronJob 변경을 포함하지 않음.
