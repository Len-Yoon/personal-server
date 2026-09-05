# N100 안전 자동 배포 설계

## 1. 목적

`main` 반영 뒤 CI가 성공한 경우에만 N100 self-hosted runner가 **CI가 검증한 정확한 revision**을 자동 배포하고, 배포 실패 시 직전 정상 revision으로 한 번 복구함. 개인 서버 운영 편의를 높이되 Secret·운영 데이터·상태 저장 서비스·K3s 제어 권한은 1차 자동 배포 범위에서 제외함.

## 2. 범위

### 포함

- 기존 GitHub Actions `CI` 성공 후 N100 배포 workflow 실행
- 변경 경로별 안전한 Compose 서비스 allowlist 결정
- CI revision 고정, 배포 전 사전 조건 확인, 배포 후 health 확인
- health 실패 시 직전 정상 revision으로 한 번 rollback 및 재검증
- 배포 실패·rollback 결과의 GitHub Actions 기록과 안전한 Telegram 상태 전달
- 배포 동시 실행 직렬화

### 제외

- Git·workflow·로그에 Secret, rclone 암호, sudo 암호를 기록하거나 전달하는 작업
- `.env`, `data/`, Kubernetes Secret, PVC 데이터의 생성·삭제·복제
- `git reset --hard`로 추적되지 않는 운영 데이터를 삭제하는 동작
- 자동 데이터 마이그레이션, 자동 PVC 복구, 자동 Caddy 공개 경로 변경
- Portal, K3s, Kubernetes RBAC·namespace·storage·monitoring·backup automation의 자동 적용
- 허용 목록 밖의 매니페스트 또는 임의 `kubectl apply -k` 실행

## 3. 선택한 방식

기존 Windows self-hosted runner 기반 workflow를 유지하고, `main` CI 성공 event만 배포 트리거로 사용함. GitHub-hosted runner의 SSH key 또는 원격 Secret은 사용하지 않음. workflow는 `workflow_run.head_sha`를 명시적으로 배포 스크립트에 전달하고, 스크립트는 해당 revision이 현재 `origin/main`의 조상인지 확인한 뒤에만 해당 revision을 checkout함. 최신 `origin/main`을 임의로 배포하지 않음.

### 3.1 1차 Compose lane

별도 `scripts/deploy-n100-safe.sh` 단일 진입점을 둠. 1차 자동 대상은 상태 migration·외부 공개 경로·관리 권한이 없는 Compose 서비스로 제한함. 기본 allowlist는 `crawler-worker`, `youtube-memo`, `book-memo`, `car-care-worker`임.

`portal-web`, `homeops-executor`, `system-agent`, Caddy는 1차 자동 대상에서 제외함. Portal은 PVC·단일 writer·파일 복원 gate와 연결되어 있고, homeops/system-agent는 운영 권한 경계가 있으므로 별도 승격 설계가 필요함.

안전한 allowlist 경로와 그 서비스의 Compose 정의만 변경된 경우에만 선택함. 그 외 runtime 경로가 함께 바뀌면 배포하지 않고 검토가 필요함을 표시함.

### 3.2 2차 K3s lane

K3s 자동 배포는 1차에 구현하지 않음. self-hosted runner에서 `sudo -n k3s`는 cluster-admin에 가까운 권한이므로, 일반 repository 코드가 임의 매니페스트를 적용하는 구조는 허용하지 않음.

2차에서는 서비스별 immutable wrapper 또는 Flux reconcile을 도입하고, image digest 고정 import → 정확한 Deployment rollout → service health 순서의 service mapping 기반 적용만 허용함. Portal writer cutover, PVC 생성·삭제·파일 복사, Secret·RBAC·monitoring·backup 도구는 계속 수동 운영 범위로 유지함.

### 3.3 변경 분류

workflow는 merge commit의 변경 파일을 아래 셋으로 분류함.

| 분류 | 조건 | 자동 동작 |
|---|---|---|
| Compose safe | allowlisted 서비스와 지정 Compose 경로만 변경 | 1차 Compose lane 실행 |
| Compose mixed | allowlisted 이외 runtime·운영 경로가 함께 변경 | 배포 차단, 검토 필요 표시 |
| K3s | K3s 관련 경로 변경 | 1차에서는 배포 생략 |
| 그 외 | 문서·테스트·비운영 경로만 변경 | 배포 생략 |

비허용 runtime 경로가 섞인 경우 배포하지 않고 workflow를 실패 처리해 운영자가 검토하도록 함. "아무 변경이나 적용"은 금지함.

## 4. 흐름

1. PR 병합으로 `main` CI를 실행함.
2. CI가 성공하면 Deploy N100 workflow가 변경 경로를 분류함.
3. N100 runner가 운영 checkout과 필수 비추적 운영 파일 존재 여부를 확인함.
4. allowlisted Compose lane의 단일 배포 진입점을 실행함.
5. 새 revision의 설정 검증과 컨테이너 health를 확인함.
6. health 실패 시 직전 정상 revision을 한 번 checkout하여 동일한 안전 대상만 재배포하고 health를 재확인함.
7. 성공·실패·rollback 결과를 GitHub Actions에 남김. Telegram 연동은 기존 relay의 안전한 status 입력 경계가 있는 경우에만 사용함.

## 5. 실패 처리와 복구

- 배포 중 새 credential, Secret, PVC, data 파일을 만들거나 지우지 않음.
- 새 revision health가 실패하면 마지막으로 health가 성공했던 revision으로 한 번만 rollback함. rollback health도 실패하면 workflow를 실패 처리하고 추가 반복을 금지함.
- 정상 revision은 health가 성공한 뒤에만 N100의 runner 전용 state 경로에 기록함. Git checkout·서비스 재배포 외의 데이터 rollback은 수행하지 않음.
- K3s lane은 1차에서 실행하지 않음. 자동 Secret 회전, RBAC 변경, PVC 삭제를 수행하지 않음.
- 배포 실패 Telegram 메시지는 서비스 이름, lane, Git commit 짧은 식별자와 실패 단계를 포함하되 비밀값·경로·명령 출력은 포함하지 않음.

## 6. 권한 경계

- GitHub workflow는 N100 self-hosted runner에서만 실행함.
- 1차 Compose lane은 Windows self-hosted runner의 기존 Docker/WSL 실행 경계만 사용함.
- K3s 자동 적용은 1차에서 사용하지 않음.
- rclone 및 systemd credential은 backup automation service만 읽으며 CD workflow는 읽거나 등록하지 않음.
- workflow dispatch는 추후 긴급 수동 재배포가 필요할 때도 `main`의 현재 commit만 대상으로 제한함.

## 7. 성공 기준

- CI 실패·취소·비-main run은 배포하지 않음.
- CI가 검증한 SHA와 N100이 checkout한 SHA가 일치함.
- 허용된 Compose 서비스 변경만 N100에서 배포됨.
- 새 revision health 실패 시 직전 정상 revision이 한 번 재배포되고 health가 다시 확인됨.
- 배포 불가 경로가 섞이면 배포 전에 실패하고, 운영 데이터와 Secret은 변경되지 않음.
- workflow·스크립트 계약 테스트와 관련 기존 테스트가 통과함.

## 8. 검증 계획

- workflow 변경 분류의 allowlist·혼합·거부 케이스 단위 테스트
- revision mismatch 거부, 정상 revision 기록, health 실패 rollback, rollback 재실패 케이스 단위 테스트
- workflow YAML 정적 검증 및 변경 범위 harness
- N100에서 allowlisted 서비스의 dry-run 성격 preflight 후, test-only 또는 의도적 health 실패 fixture로 rollback 동작 확인
