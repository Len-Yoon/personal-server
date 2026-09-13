# 분기 K3s SRE 점검 설계

## 목적

N100의 systemd 사용자 서비스가 제한된 `sudo -n k3s` 호출을 실행하지 못하는 문제를 제거하고, 분기마다 Kubernetes 운영 상태·Portal 가용성·Portal PVC 백업 증적·격리 복구 실습을 Telegram SRE relay로 보고함.

## 범위

- K3s CronJob이 `Asia/Seoul` 기준 매 분기 첫날 03:30에 단일 실행됨.
- 점검 결과는 `monitoring/sre-telegram-quarterly-audit-status` ConfigMap에 기록됨.
- 백업 증적은 `personal-server/portal-pvc-backup-evidence`를 24시간 이내, `source_runtime=k3s-pvc` 조건으로 fail-closed 검증함.
- 복구 실습은 전용 `sre-recovery-lab` namespace 안에서만 실행·정리됨.
- 기존 사용자 systemd timer는 설치 도구가 중지·비활성화하여 중복 실행을 방지함.

## 제외 범위

- Compose 컨테이너 상태는 CronJob 결과에 포함하지 않음. Pod가 Docker socket, hostPath, privileged 권한 또는 노드 셸 접근을 획득하지 않음.
- Portal PVC, Secret, Caddyfile, Tunnel ingress, Compose Portal writer를 수정하지 않음.
- Telegram 비밀값을 읽거나 생성하지 않음. 기존 relay가 ConfigMap 상태를 전달함.

## 보안 경계

- ServiceAccount는 노드 조회, Portal Deployment 조회, 백업 증적 조회, 전용 복구 namespace의 Deployment/Pod/exec/events 조작, 단일 monitoring ConfigMap patch만 허용함.
- CronJob Pod는 non-root, 읽기 전용 루트 파일시스템, RuntimeDefault seccomp, 모든 capability drop, `allowPrivilegeEscalation: false`로 실행함.
- CronJob은 `Forbid` 동시 실행, `backoffLimit: 0`, deadline, TTL을 적용함.

## 성공 기준

1. 이전 사용자 timer가 비활성화되고 CronJob만 자동 실행 상태가 됨.
2. runner는 `sudo`, `docker`, hostPath 없이 실행됨.
3. 각 점검 단계 성공·실패와 관계없이 상태 ConfigMap 기록을 시도하며, 실패 상태는 fail-closed로 남김.
4. 수동 Job이 성공하고, relay 전달 상태 및 외부 `/health` 3회 연속 HTTP 200으로 운영 검증됨.
