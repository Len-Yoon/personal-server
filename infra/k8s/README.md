# K3s 운영

N100의 K3s는 현재 Portal과 모니터링 운영에 사용함. 이 문서는 실제 운영 도구의 진입점만 정리하며, Secret 값·비밀번호·token은 출력하거나 문서화하지 않음.

## 현재 구성

| Namespace | 구성 | 역할 |
|---|---|---|
| `personal-server` | `portal-web`, Service, Portal PVC | Portal·파일함·관리자·포트폴리오 |
| `monitoring` | Prometheus, Grafana, Alertmanager, SRE Telegram relay | 상태 수집·시각화·경고 전달 |

Portal은 K3s PVC를 상태 저장소로 사용하며, Compose `portal-web`은 동시에 실행하지 않음. Caddy는 `host.docker.internal:30080` NodePort를 통해 K3s Portal로 전달함. K3s runtime에서는 `.env`의 `PORTAL_UPSTREAM=host.docker.internal:30080` 설정이 필요하며, Compose runtime에서는 이 값을 `portal-web:8000`으로 유지하거나 비워 Compose 기본값을 사용함. Portal cutover는 이 값을 자동으로 전환하므로 수동 변경 대신 해당 절차를 사용함.

## 빠른 상태 확인

```bash
sudo k3s kubectl get nodes
sudo k3s kubectl -n personal-server get deploy,pod,pvc
sudo k3s kubectl -n monitoring get pod,pvc
bash infra/k8s/tools/sre-health-audit.sh
```

## Grafana와 Prometheus

Grafana는 N100 내부 전용임. 필요할 때 아래 명령을 실행한 뒤 브라우저에서 `http://127.0.0.1:3000`을 열고, 종료할 때 `Ctrl+C`를 누름.

```bash
sudo k3s kubectl -n monitoring port-forward --address 127.0.0.1 service/personal-server-monitoring-grafana 3000:80
```

설치 상태를 점검할 때는 다음을 사용함.

```bash
bash infra/k8s/tools/monitoring-preflight.sh
bash infra/k8s/tools/monitoring-verify.sh
```

`monitoring-install.sh --apply`와 제거 명령은 운영자 승인 후에만 실행함. Grafana와 Prometheus PVC는 기본 제거에서 보존함.

## Telegram SRE 알림

Alertmanager 경고는 `sre-telegram-relay`를 통해 Telegram으로 전달함. Secret 값은 N100의 승인된 Secret 관리 절차로만 관리함.

- 승인된 bearer 값은 runtime Secret 키 `alertmanager_auth_token`에만 입력함.
- 임시 Alertmanager 설정 파일에는 `credentials_file` 경로만 유지하며 bearer 값은 포함하지 않는다.

```bash
bash infra/k8s/tools/sre-telegram-preflight.sh --alertmanager-config-file <0600-설정파일>
bash infra/k8s/tools/sre-telegram-verify.sh
```

relay, PrometheusRule, RBAC 경계, Prometheus target 상태를 검증하며 Secret 값은 읽지 않음.

## Portal PVC 백업

Portal PVC 백업은 K3s CronJob 경로로 전환 준비됨. CronJob은 `suspend: true` 상태로 배포되며, 승인된 Secret Manager 또는 SOPS/age 절차로 사전 시딩된 runtime Secret과 runner image가 준비되기 전에는 활성화하지 않음. 저장소 도구는 Secret 값·rclone 설정·age identity를 생성·입력·출력하지 않음.

전환 전에는 아래 읽기 점검과 client-side render를 실행함. `--preflight`는 Secret **이름과 key 이름만** 확인하고, Portal PVC가 `Bound`·`ReadWriteOnce` mount 계약을 충족하는지 확인함.

```bash
bash infra/k8s/tools/portal-pvc-backup-cronjob.sh --preflight
bash infra/k8s/tools/portal-pvc-backup-cronjob.sh --render
bash infra/k8s/tools/portal-pvc-backup-verify.sh --check
```

운영자 승인 후 `--apply`는 suspended CronJob과 최소 RBAC만 적용함. 기존 systemd timer를 비활성화하고, 수동 실행의 백업·복원 검증 및 Telegram 결과를 확인한 뒤에만 `--activate`로 CronJob을 해제함. 두 scheduler가 동시에 활성화되는 상태는 허용하지 않음.

```bash
bash infra/k8s/tools/portal-pvc-backup-cronjob.sh --apply
bash infra/k8s/tools/portal-pvc-backup-cronjob.sh --status
# 기존 timer가 inactive이고 수동 검증이 성공한 뒤에만 실행함.
bash infra/k8s/tools/portal-pvc-backup-cronjob.sh --activate
```

CronJob은 매일 03:00 KST에 실행되며, `Forbid` 동시 실행 제한·실패 재시도 없음·read-only PVC mount·고정 ServiceAccount 권한을 사용함. 성공·변경 없음·실패·복원 검증 실패는 Telegram SRE relay로 상태 전환을 전달함. 실행 중 백업이 중단되면 300초 종료 유예 안에서 Portal replica 복구를 시도하며, 복구 상태를 확인해야 함.

## 분기 SRE 점검 자동화

분기 SRE 점검 자동화는 운영자 설치 전 상태이며 현재 N100에서 활성화되지 않음. 저장소 구현 또는 병합만으로 timer가 활성화되지 않으며, 별도 운영 승인 후 N100 운영자가 아래 `--install`을 직접 실행해야 함. 설치 전 `--preflight`가 통과해야 하며, 설치 과정에서 Secret·token·Telegram chat ID·rclone 자격 증명을 생성하거나 복제하지 않음. 기존에 승인된 runtime 자격 증명만 service의 `LoadCredentialEncrypted` 경로로 사용함.

```bash
bash infra/k8s/tools/quarterly-sre-audit-automation.sh --preflight
# 별도 운영 승인 후 N100에서 실행함.
bash infra/k8s/tools/quarterly-sre-audit-automation.sh --install
```

설치 후 timer는 N100 사용자 `systemd` 기준 매년 1·4·7·10월 1일 03:30 KST에 1회 실행되며, `Persistent=true`로 예정 시각을 놓친 경우의 catch-up을 허용함. timer와 수동 실행의 동시 실행은 non-blocking lock으로 차단함. 수동 점검은 다음 단일 명령만 사용함.

```bash
bash infra/k8s/tools/quarterly-sre-audit-automation.sh --run
```

`--run`은 다음 세 점검을 각각 수행하고 한 단계가 실패해도 나머지 점검을 계속 수행함.

| 점검 | 실행 도구 | 범위 |
|---|---|---|
| 상태 점검 | `sre-health-audit.sh` | 운영 상태 확인 |
| 백업 검증 상태 확인 | `portal-pvc-backup-verify.sh --check` | Portal PVC 백업 검증 상태 확인 |
| 격리 Pod 복구 훈련 | `sre-pod-recovery-lab.sh --run` | 임시 격리 namespace의 Pod만 대상으로 자동 정리함 |

이 과정은 Portal·Caddy·Cloudflare Tunnel·Compose 서비스를 stop, restart, scale 또는 rollout하지 않으며, 운영 데이터와 Portal PVC를 변경하지 않음. 결과는 실행 ID, 완료 시각, 종합 상태와 세 단계 상태만 `monitoring/sre-telegram-quarterly-audit-status` ConfigMap에 기록함. 기존 `sre-telegram-relay`는 Telegram 성공 응답이 확인될 때까지 재시도하며, 응답 유실 시 드물게 중복 메시지가 수신될 수 있음. 명령 출력 전문·namespace/Pod 식별자·파일 경로·내부 IP·Secret 값은 전달하지 않음.

실제 적용 시 운영자는 수동 `--run` 1회를 실행한 뒤 다음 세 가지를 모두 직접 확인해야 함.

1. ConfigMap 기록에 세 단계 결과와 종합 결과가 남았는지 확인함.
2. 격리 namespace의 임시 리소스가 정리되었는지 확인함.
3. 기존 SRE Telegram relay를 통해 요약 메시지가 수신되었는지 확인함.

Telegram 수신을 확인하기 전에는 분기 점검 적용 또는 알림 정상으로 판단하지 않음. 확인 명령은 다음과 같으며, 출력에 Secret 값을 포함하지 않도록 함.

```bash
bash infra/k8s/tools/quarterly-sre-audit-automation.sh --status
```

## Portal 전환과 복구

Portal 전환은 명시적 운영 작업임. 백업 증거를 먼저 확인하고, 전환과 공개 경로 변경을 분리해 실행함.

```bash
bash infra/k8s/tools/portal-backup-verify.sh
bash infra/k8s/tools/portal-cutover.sh --go
bash infra/k8s/tools/portal-cutover.sh --switch-caddy
```

실패하면 실행기가 Compose Portal과 이전 공개 경로를 복구하도록 설계됨. 결과는 마지막 `portal_cutover=PASS|FAIL`로 판단함.

## SRE Pod 자동복구 실습

운영자 전용 실습은 `sre-pod-recovery-lab.sh`로 실행하며, production Portal과 별도의 namespace를 사용함.
실습 리소스는 sre-recovery-lab-<run-id> namespace에만 생성된다.

```bash
bash infra/k8s/tools/sre-pod-recovery-lab.sh --run
bash infra/k8s/tools/sre-pod-recovery-lab.sh --cleanup <run-id>
```

성공 결과에는 liveness 실패 뒤 `restartCount`가 증가한 사실과 Pod가 `Ready` 조건으로 복구됨이 포함됨.
이 실습은 다른 namespace, Portal, Compose, Caddy, scheduler를 변경하거나 재시작하지 않는다.
운영 대상은 변경하지 않는다.

## 운영 경계

- Secret 생성·값 입력·출력은 이 저장소 도구의 범위 밖임.
- Portal PVC·K3s·Caddy·Cloudflare Tunnel은 N100 안전 자동 배포 대상이 아님.
- 임시 Pod 자동복구 실습은 `sre-pod-recovery-lab.sh`만 사용하며 production Portal을 변경하지 않음.

## Portal 보안 smoke와 전환 경계

`portal-secret-shadow-smoke.sh`는 `isolated manual smoke` 절차이며, 운영 Portal과 분리된 임시 namespace에서만 실행함. 이 smoke는 `optional HomeOps/portfolio` 설정, `data copy`, `Caddy routing`, `actual cutover`을 검증하지 않음. 실제 데이터 이동과 공개 경로 전환은 별도 승인·백업·복구 검증이 필요한 운영 작업임.

Portal cutover는 `operator-only` 절차임. 실행 전 `K3s Secret encryption`과 `backup evidence`를 확인하고, `Compose writer` 단일 소유권 및 `PORTAL_BACKUP_MAX_AGE_SECONDS=86400` 유효성을 검증함. 데이터 매니페스트는 `sha256`으로 비교하고, 임시 Secret 파일은 `0600`으로 제한함. 공개 경로 변경과 정리는 별도 호출함.

```bash
bash infra/k8s/tools/portal-cutover.sh --rollback-caddy
bash infra/k8s/tools/portal-cutover.sh --cleanup-rolledback
```
