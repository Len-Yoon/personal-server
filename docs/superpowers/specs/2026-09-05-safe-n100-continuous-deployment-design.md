# N100 안전 자동 배포 설계

## 1. 목적

`main` 반영 뒤 CI가 성공한 경우에만 N100 self-hosted runner가 **CI가 검증한 정확한 revision**을 자동 배포하고, 배포 실패 시 직전 정상 revision으로 한 번 복구함. 개인 서버 운영 편의를 높이되 Secret·운영 데이터·상태 저장 서비스·K3s 제어 권한은 1차 자동 배포 범위에서 제외함.

## 2. 범위

### 포함

- `main` push의 정확한 `before..github.sha` 전체 변경 범위 분류
- GitHub API로 동일 `github.sha`의 `CI` 성공을 확인한 뒤 N100 배포 runner 할당
- 변경 경로별 안전한 Compose 서비스 allowlist 결정
- SHA release source archive 생성, 임시 Compose override를 통한 제한된 빌드·health 확인
- health 실패 시 직전 정상 SHA로 한 번 rollback 및 재검증
- 배포 단계·실패·rollback 결과의 GitHub Actions 기록
- 배포 동시 실행 직렬화

### 제외

- Git·workflow·로그에 Secret, rclone 암호, sudo 암호를 기록하거나 전달하는 작업
- `.env`, `data/`, Kubernetes Secret, PVC 데이터의 생성·삭제·복제
- `git reset --hard`로 추적되지 않는 운영 데이터를 삭제하는 동작
- 자동 데이터 마이그레이션, 자동 PVC 복구, 자동 Caddy 공개 경로 변경
- Portal, K3s, Kubernetes RBAC·namespace·storage·monitoring·backup automation의 자동 적용
- Telegram CD 성공·실패·rollback 상태 메시지 및 SRE relay 변경
- 허용 목록 밖의 매니페스트 또는 임의 `kubectl apply -k` 실행

## 3. 선택한 방식

기존 Windows self-hosted runner 기반 workflow를 유지하되, `main`의 push event를 배포 후보로 사용함. GitHub-hosted `changes` job은 event의 `before`와 `github.sha`가 유효한 SHA인지 확인하고 그 전체 범위를 분류함. 같은 job은 기본 `GITHUB_TOKEN`의 `actions: read` 권한으로 GitHub Actions API를 조회하여, 정확히 `github.sha`와 일치하는 `CI` push run이 `success`가 될 때까지 제한 시간 동안 대기함. 정확한 CI run·범위 정보를 얻지 못하면 N100 runner를 할당하지 않고 실패 처리함.

N100에서는 운영 checkout을 변경하지 않음. 스크립트는 `expected_sha`가 `origin/main`의 조상인지 확인한 뒤, 다시 `git rev-parse origin/main`이 `expected_sha`와 정확히 일치하는지 확인하여 stale SHA를 거부함. 이후 안전 state 경로에 SHA별 임시 release directory를 만들고, 선택된 서비스의 `Dockerfile`, `requirements.txt`, `app/`만 `git archive`로 추출함. 임시 Compose override는 선택된 서비스의 build context와 읽기 전용 `/app` bind mount만 release directory로 바꿈. 기본 Compose 파일, `.env`, `data/`는 운영 project root에 그대로 둠.

### 3.1 1차 Compose lane

별도 `scripts/deploy-n100-safe.sh` 단일 진입점을 둠. 1차 자동 대상은 상태 migration·외부 공개 경로·관리 권한이 없는 Compose 서비스로 제한함. 기본 allowlist는 `crawler-worker`, `youtube-memo`, `book-memo`, `car-care-worker`임.

`portal-web`, `homeops-executor`, `system-agent`, Caddy는 1차 자동 대상에서 제외함. Portal은 PVC·단일 writer·파일 복원 gate와 연결되어 있고, homeops/system-agent는 운영 권한 경계가 있으므로 별도 승격 설계가 필요함.

안전한 allowlist 경로와 그 서비스의 Compose 정의만 변경된 경우에만 선택함. 그 외 runtime 경로가 함께 바뀌면 배포하지 않고 검토가 필요함을 표시함.

### 3.2 2차 K3s lane

K3s 자동 배포는 1차에 구현하지 않음. self-hosted runner에서 `sudo -n k3s`는 cluster-admin에 가까운 권한이므로, 일반 repository 코드가 임의 매니페스트를 적용하는 구조는 허용하지 않음.

2차에서는 서비스별 immutable wrapper 또는 Flux reconcile을 도입하고, image digest 고정 import → 정확한 Deployment rollout → service health 순서의 service mapping 기반 적용만 허용함. Portal writer cutover, PVC 생성·삭제·파일 복사, Secret·RBAC·monitoring·backup 도구는 계속 수동 운영 범위로 유지함.

### 3.3 변경 분류

workflow는 `main` push event의 `before..github.sha` 전체 변경 파일을 아래 셋으로 분류함.

| 분류 | 조건 | 자동 동작 |
|---|---|---|
| Compose safe | allowlisted 서비스와 지정 Compose 경로만 변경 | 1차 Compose lane 실행 |
| Compose mixed | allowlisted 이외 runtime·운영 경로가 함께 변경 | 배포 차단, 검토 필요 표시 |
| K3s | K3s 관련 경로 변경 | 1차에서는 배포 생략 |
| 그 외 | 문서·테스트·비운영 경로만 변경 | 배포 생략 |

`before`가 없거나 zero SHA이면 정확한 전체 범위를 보장할 수 없으므로 fail-closed 처리함. 비허용 runtime 경로가 섞인 경우도 배포하지 않고 workflow를 실패 처리해 운영자가 검토하도록 함. "아무 변경이나 적용"은 금지함.

## 4. 흐름

1. PR 병합 또는 직접 push로 `main` push event가 발생함.
2. GitHub-hosted job이 `before..github.sha` 전체 범위를 확보하고, GitHub API에서 동일 `github.sha`의 `CI` push run 성공을 제한 시간 동안 확인함.
3. 정확한 범위·CI 성공을 확인한 경우에만 변경 경로를 분류하고 N100 runner를 할당함.
4. N100 runner가 운영 project root와 필수 비추적 운영 파일 존재 여부를 확인하고, `expected_sha == origin/main` stale 보호를 통과시킴.
5. 선택 서비스 source만 SHA release directory에 `git archive`로 생성하고, 임시 override를 사용해 단일 Compose lane을 실행함.
6. health script가 최대 90초 동안 2초 간격으로 요청 컨테이너의 `healthy` 상태와 loopback endpoint를 모두 확인함.
7. health 실패 시 state의 직전 정상 SHA도 동일한 source archive·임시 override 방식으로 한 번만 rollback하고 health를 재확인함.
8. 성공·실패·rollback 결과는 GitHub Actions stage와 log에 남김. Phase 1에서 Telegram CD 메시지는 보내지 않음.

## 5. 실패 처리와 복구

- 배포 중 새 credential, Secret, PVC, data 파일을 만들거나 지우지 않음.
- 새 revision health가 실패하면 마지막으로 health가 성공했던 revision으로 한 번만 rollback함. rollback health도 실패하면 workflow를 실패 처리하고 추가 반복을 금지함.
- stale SHA, CI 성공 미확인, `before` 범위 미확보는 Docker·archive 실행 전 실패 처리함.
- 정상 revision은 health가 성공한 뒤에만 N100의 runner 전용 state 경로에 기록함. rollback은 별도 SHA release source archive와 서비스 재배포만 수행하며, 운영 checkout·데이터 rollback은 수행하지 않음.
- K3s lane은 1차에서 실행하지 않음. 자동 Secret 회전, RBAC 변경, PVC 삭제를 수행하지 않음.
- Telegram CD 메시지는 Phase 1 범위에서 명시적으로 제외함. 운영자 신호는 GitHub Actions의 job stage와 log임.

## 6. 권한 경계

- GitHub-hosted `changes` job은 기본 `GITHUB_TOKEN`의 `contents: read`, `actions: read`만 사용해 정확한 CI run을 조회함.
- N100 self-hosted runner는 `changes` job 성공 뒤에만 실행함.
- 1차 Compose lane은 Windows self-hosted runner의 기존 Docker/WSL 실행 경계만 사용함.
- K3s 자동 적용은 1차에서 사용하지 않음.
- rclone 및 systemd credential은 backup automation service만 읽으며 CD workflow는 읽거나 등록하지 않음.
- workflow dispatch는 1차에 사용하지 않음.

## 7. 성공 기준

- `before` 범위가 유효하고 정확한 `github.sha`의 CI가 성공한 경우에만 배포함.
- `expected_sha`가 `origin/main`의 조상이면서 현재 `origin/main`과 정확히 일치하지 않으면 archive·Docker 실행 전에 거부됨.
- 운영 project checkout은 변경되지 않고, SHA release source의 allowlisted 파일만 빌드·`/app` mount에 사용됨.
- 허용된 Compose 서비스 변경만 N100에서 배포됨.
- 컨테이너가 `starting` 상태인 동안에는 통과하지 않으며, 최대 90초 내 healthy와 loopback health가 모두 확인된 뒤에만 성공함.
- 새 revision health 실패 시 직전 정상 revision이 한 번 재배포되고 health가 다시 확인됨.
- 배포 불가 경로가 섞이면 배포 전에 실패하고, 운영 데이터와 Secret은 변경되지 않음.
- Telegram CD 메시지를 보내지 않고 GitHub Actions stage가 운영자 신호가 됨.
- workflow·스크립트 계약 테스트와 관련 기존 테스트가 통과함.

## 8. 검증 계획

- workflow의 `before..github.sha` 전체 범위·정확한 CI SHA API 대기·N100 runner 선행 차단 계약 테스트
- stale SHA 거부와 Docker 미호출, release archive의 선택 파일 제한, 임시 override 적용 계약 테스트
- health `starting → healthy` poll과 timeout·rollback 재시도 제한 단위 테스트
- workflow YAML 정적 검증 및 변경 범위 harness
- N100에서 allowlisted 서비스의 dry-run 성격 preflight 후, test-only 또는 의도적 health 실패 fixture로 rollback 동작 확인
