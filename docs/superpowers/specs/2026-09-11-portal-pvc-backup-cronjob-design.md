# Portal PVC Backup CronJob 전환 설계

## 목적

WSL 사용자 systemd credential host-key 권한 제약으로 실패한 Portal PVC 백업 자동화를 K3s CronJob 하나로 전환함. 백업과 원격 복원 검증을 매일 실행하고 기존 Telegram SRE relay로 결과를 전달함.

## 범위와 안전 경계

- Portal PVC는 CronJob Pod에만 read-only로 mount하고, Portal writer가 정지된 뒤에만 읽음.
- Portal writer는 백업 일관성 확보에 필요한 동안에만 0 replica로 축소하고, 성공·실패·중단 모두 원래 1 replica 복구를 보장함.
- Portal 운영 Secret, PVC, Caddyfile, Cloudflare Tunnel ingress, Compose Portal writer는 수정·삭제·재생성하지 않음.
- CronJob은 `portal-pvc-backup-verify.sh --go`만 실행함.
- 자동 실행 주체는 CronJob 하나만 사용하며 `concurrencyPolicy: Forbid`와 기존 파일 잠금을 함께 적용함.
- 기존 systemd user timer/service와 encrypted credential은 CronJob의 실제 정상 동작이 확인된 뒤 제거 대상으로만 다룸. 이번 전환에서 자동 삭제하지 않음.

## 자격 증명 경계

- 새 `portal-pvc-backup-runtime` Kubernetes Secret은 사용자가 승인된 Secret Manager 또는 SOPS/age 절차로 사전 시딩함.
- Secret에는 rclone 설정·rclone 설정 암호·age recipient·age identity만 파일 형태로 포함함.
- 저장소, 스크립트, ConfigMap, 로그, 환경 변수에는 해당 값 또는 값의 복제본을 저장·출력하지 않음.
- CronJob은 Secret을 read-only volume으로 mount하고, rclone 설정과 password-command에는 파일 경로만 전달함.
- 구현 도구는 Secret을 생성·변경·출력하지 않으며, 배포 전 존재 여부와 필요한 key 이름만 확인함.

## 실행 구조

1. 매일 KST 기준 지정 시각에 `CronJob`이 backup runner image를 실행함.
2. runner는 in-cluster ServiceAccount token과 `kubectl`로 Portal Deployment만 제한적으로 제어하고, 자체 read-only PVC mount로 기존 backup verify 도구를 `--go`로 실행함.
3. `portal-pvc-backup-verify.sh`는 host 전용 `sudo k3s kubectl` 호출 대신 명시적 실행 모드에서 in-cluster `kubectl`을 사용함. in-cluster mode는 host runtime marker를 요구하지 않으며, 기존 host 수동 실행 모드는 유지함.
4. runner는 verifier 내부에서 비밀값 없는 evidence ConfigMap을 갱신하고, 결과를 고정 schema의 `monitoring/sre-telegram-backup-status` ConfigMap에 patch함.
5. 기존 Telegram relay가 해당 ConfigMap의 새로운 run ID를 한 번만 전달함. CronJob에는 Telegram 자격 증명을 주입하지 않음.

## 권한 설계

`personal-server` namespace의 backup ServiceAccount에는 다음만 허용함.

- `persistentvolumeclaims`: `get` (두 Portal PVC 이름으로 제한)
- `deployments`: `get`, `watch` (Portal Deployment 이름으로 제한)
- `deployments/scale`: `get`, `patch` (Portal Deployment 이름으로 제한)

`personal-server` namespace에는 비밀값 없는 backup evidence ConfigMap을 사전 생성하고 `get`, `patch`만 허용함. `monitoring` namespace에서는 고정 ConfigMap `sre-telegram-backup-status`에만 `get`, `patch`를 허용함. Secret 읽기 RBAC는 부여하지 않으며 kubelet의 read-only Secret volume mount만 사용함.

## runner image

- backup runner는 저장소에서 이미 사용·검증된 base image digest를 사용하고 `kubectl`, `rclone`, `age`, `sqlite3`, `bash`, `tar` 및 기존 backup 도구를 포함함.
- non-root, read-only root filesystem, 모든 Linux capability drop, `allowPrivilegeEscalation: false`를 적용함.
- N100 배포 시 이미지를 명시 태그로 build/import하고 `imagePullPolicy: Never`를 사용함.

## 오류 처리와 복구

- CronJob의 `backoffLimit: 0`, `activeDeadlineSeconds`, `ttlSecondsAfterFinished`, Portal 복구에 충분한 `terminationGracePeriodSeconds`를 설정함.
- timeout·signal·백업 실패 시 기존 cleanup trap이 Portal writer 복구를 우선 수행함.
- 결과 상태는 `completed`, `unchanged`, `failed`, `restore_failed`만 사용하며 stage는 allow-list 형식만 기록함.
- Secret 부재, 권한 부족, image 누락은 Job 실패로 기록하며 Secret 내용은 어떠한 출력에도 포함하지 않음.

## 검증 기준

- CronJob, ServiceAccount, RBAC, Secret reference manifest의 정적 계약 테스트 통과함.
- N100에서 두 RWO PVC를 Portal과 CronJob Pod가 같은 단일 node에 동시에 read-only attach할 수 있는지, 실제 데이터를 읽지 않는 mount capability probe로 확인함. 불가능하면 배포하지 않고 설계를 재검토함.
- host·in-cluster 실행 모드의 backup 도구 단위 테스트 통과함.
- 기존 Telegram relay의 backup status 전달·중복 억제 테스트 통과함.
- manifest client dry-run 및 backup runner image build 검사 통과함.
- 적용 전후 `https://len.pe.kr/health`를 10초 간격 3회 호출해 모두 HTTP 200 확인함.
- 실제 배포는 사전 시딩 Secret 확인 및 사용자 승인 후에만 수행함.

## 비목표

- 백업 Secret 값 생성·보관·마이그레이션 자동화
- Portal PVC·운영 데이터 변경 또는 복원 실행
- Cloudflare Tunnel·Caddy·Portal 배포 설정 변경
- 자동 재부팅·서버 복구 동작 추가
