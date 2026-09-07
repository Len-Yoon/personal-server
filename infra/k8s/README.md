# K3s 운영

N100의 K3s는 현재 Portal과 모니터링 운영에 사용함. 이 문서는 실제 운영 도구의 진입점만 정리하며, Secret 값·비밀번호·token은 출력하거나 문서화하지 않음.

## 현재 구성

| Namespace | 구성 | 역할 |
|---|---|---|
| `personal-server` | `portal-web`, Service, Portal PVC | Portal·파일함·관리자·포트폴리오 |
| `monitoring` | Prometheus, Grafana, Alertmanager, SRE Telegram relay | 상태 수집·시각화·경고 전달 |

Portal은 K3s PVC를 상태 저장소로 사용하며, Compose `portal-web`은 동시에 실행하지 않음. Caddy는 `host.docker.internal:30080` NodePort를 통해 K3s Portal로 전달함.

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

Portal이 K3s runtime일 때 N100 사용자 `systemd` timer가 암호화 백업과 복원 검증을 수행함. rclone 설정 암호는 N100 사용자 전용 암호화 credential로만 보관하며, sudo 비밀번호와 함께 저장하지 않음.

```bash
bash infra/k8s/tools/portal-pvc-backup-automation.sh --status
bash infra/k8s/tools/portal-pvc-backup-verify.sh --check
```

백업 성공·변경 없음·실패·복원 검증 실패는 Telegram SRE relay로 알림. 실행 중인 백업을 중단하면 Portal을 즉시 다시 1개 replica로 복구한 뒤 상태를 확인해야 함.

## Portal 전환과 복구

Portal 전환은 명시적 운영 작업임. 백업 증거를 먼저 확인하고, 전환과 공개 경로 변경을 분리해 실행함.

```bash
bash infra/k8s/tools/portal-backup-verify.sh
bash infra/k8s/tools/portal-cutover.sh --go
bash infra/k8s/tools/portal-cutover.sh --switch-caddy
```

실패하면 실행기가 Compose Portal과 이전 공개 경로를 복구하도록 설계됨. 결과는 마지막 `portal_cutover=PASS|FAIL`로 판단함.

## 운영 경계

- Secret 생성·값 입력·출력은 이 저장소 도구의 범위 밖임.
- Portal PVC·K3s·Caddy·Cloudflare Tunnel은 N100 안전 자동 배포 대상이 아님.
- 임시 Pod 자동복구 실습은 `sre-pod-recovery-lab.sh`만 사용하며 production Portal을 변경하지 않음.
