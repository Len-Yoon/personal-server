# 분기 SRE 점검 및 Telegram 결과 보고 설계

## 문서 정보

| 항목 | 내용 |
|---|---|
| 목적 | 운영 서비스 중단 없이 분기별 SRE 핵심 점검을 실행하고 결과를 기존 Telegram SRE relay로 전달함 |
| 실행 주기 | 매년 1·4·7·10월 1일 03:30 KST, N100 사용자 `systemd` timer 기준 |
| 기준 자료 | `sre-health-audit.sh`, `sre-pod-recovery-lab.sh`, Portal PVC 백업 검증 자동화, `sre-telegram-relay` |
| 적용 범위 | N100 WSL 사용자 timer, K3s monitoring ConfigMap, 기존 SRE Telegram relay |

## 핵심 요약

분기 점검은 실제 Portal·Caddy·Cloudflare Tunnel·Compose 서비스를 중지하거나 재시작하지 않음. 읽기 전용 상태 점검, Portal 백업 검증 상태 확인, 격리 namespace의 Pod 복구 훈련을 한 번 실행함. 각 단계 결과를 하나의 비밀값 없는 ConfigMap에 기록하고, 기존 relay가 새 실행 ID를 Telegram에 최소 1회 전달하도록 재시도함.

## 구성 및 데이터 흐름

1. N100 사용자 controller의 `--install`이 검증된 systemd user unit을 렌더·활성화하고, timer가 분기별 service를 실행함.
2. controller가 아래 순서로 실행하되, 한 단계 실패 후에도 나머지 단계를 수행하여 종합 결과를 만듦.
   - `infra/k8s/tools/sre-health-audit.sh`
   - `infra/k8s/tools/portal-pvc-backup-verify.sh --check`
   - `infra/k8s/tools/sre-pod-recovery-lab.sh --run`
3. controller는 `monitoring/sre-telegram-quarterly-audit-status` ConfigMap에 안전한 실행 ID, 완료 시각, 각 단계 상태, 종합 상태만 기록함.
4. `sre-telegram-relay`는 ConfigMap을 읽어 새 실행 ID를 Telegram에 전송한 뒤 성공 응답을 받은 경우에만 전달 ID를 기존 relay 상태 ConfigMap에 제한된 개수로 보관함. 전송 또는 상태 저장 실패 시 기존 polling loop의 상한형 지수 backoff(1→2→4→…→30초)로 재시도함.

## 안전 경계

| 구분 | 설계 |
|---|---|
| 실제 서비스 | Portal, Caddy, Tunnel, Compose 서비스에 stop, restart, scale, rollout을 수행하지 않음 |
| 복구 훈련 | `sre-pod-recovery-lab-*` 임시 namespace 안의 digest 고정 busybox Pod만 비정상 상태로 만들고 자동 정리함. Pod는 non-root, privilege escalation 금지, service account token 미마운트로 실행함 |
| 권한 | 기존 제한된 `sudo -n k3s`만 사용함. 새 sudo 권한, Secret, token, chat ID를 만들거나 저장하지 않음 |
| 동시 실행 | systemd service 수준의 non-blocking lock으로 timer catch-up과 수동 실행의 중복을 차단함 |
| 실행 제한 | service 시작·종료 시간 제한을 두어 장시간 실행이 남지 않도록 함 |
| 설치 경로 | controller의 `--preflight`과 `--install`만 unit을 렌더·활성화함. 설치 전 `systemd-analyze --user verify`가 통과해야 하며, rclone credential 생성·복제는 수행하지 않음 |
| Telegram | Bot token과 chat ID는 relay의 기존 runtime Secret만 사용함. controller·timer·로그에 비밀값을 기록하지 않음 |
| 중복 전달 | 저장 성공 후에는 실행 ID 기준 최대 128건을 중복 억제함. Telegram 성공 응답 뒤 상태 저장 응답이 유실·실패하면 다음 시도에서 드물게 동일 결과가 중복 전달될 수 있음 |
| 실패 처리 | ConfigMap 기록 실패 시 controller는 실패 종료함. Telegram 전송 또는 relay 상태 저장 실패는 relay health 실패로 남기고, 기존 polling loop의 상한형 지수 backoff로 재시도함. 1분·5분·15분·1시간 별도 scheduler는 추가하지 않음 |

## Telegram 결과 형식

Telegram에는 다음 정보만 포함함.

```
[분기 SRE 점검 완료] 또는 [분기 SRE 점검 실패]
상태 점검: 통과 또는 실패
백업 검증 상태: 통과 또는 실패
격리 Pod 복구 훈련: 통과 또는 실패
조치: 실패 항목이 있으면 운영 문서에 따라 확인 필요
```

명령 출력 전문, namespace 이름, Pod 이름, token, chat ID, 파일 경로, 내부 IP는 포함하지 않음.

## 검증 및 적용 기준

- controller와 relay의 단위·계약 테스트를 추가함.
- systemd service/timer 템플릿이 KST 분기 주기·단일 실행·실행 시간 제한·제한된 명령만 사용하는지 검증함. controller 설치가 unit 검증 이전에는 timer를 활성화하지 않는지 검증함.
- K3s RBAC은 분기 상태 ConfigMap 한 개만 읽을 수 있도록 제한함.
- 실제 N100 적용 전에는 수동 1회 실행에서 ConfigMap 기록, 격리 namespace 정리, Telegram 수신을 모두 확인함.
- N100 자동 적용은 안전 자동배포 대상이 아니므로, 저장소 병합 뒤 별도 사용자 승인으로만 수행함.

## 제외 범위

- 실제 Portal·Tunnel·Caddy 장애 유발 훈련
- Telegram Secret·토큰·채팅 ID 생성 또는 변경
- CronJob 추가 및 기존 백업 timer 변경
- Cloudflare 구성, PVC 데이터, Compose Portal writer 변경
