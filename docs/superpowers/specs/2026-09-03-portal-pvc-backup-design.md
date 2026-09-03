# Portal K3s PVC 백업 설계

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | Portal K3s PVC 백업 설계 |
| 작성일 | 2026-09-03 |
| 목적 | K3s로 전환된 Portal의 실제 영속 데이터를 일관성 있게 암호화 백업하고 복원 검증하는 운영 절차를 정의함 |
| 대상 | `personal-server` 네임스페이스의 Portal Deployment 및 PVC 2개 |
| 제외 | 서버 기동 스크립트, 스케줄러, Caddy, Cloudflare Tunnel, 다른 서비스 및 기존 원격 archive 삭제 |

## 2. 핵심 요약

Portal이 K3s runtime(`portal-runtime.mode=k3s`)일 때에는 로컬 `data/`가 실제 writer가 아니므로 기존 로컬 백업 도구를 사용하지 않음. 전용 operator-only 도구가 Portal Deployment를 잠시 0 replica로 축소해 writer를 완전히 멈춘 뒤, 실제 PVC 두 개를 임시 reader Pod에 마운트하여 stage 영역으로 복사함.

stage 데이터는 age로 암호화하여 Google Drive에 업로드하고, 원격 artifact를 별도 임시 경로에 다시 내려받아 복호화함. 원본·복원본의 파일 manifest, HomeOps SQLite `quick_check`, 필수 경로를 모두 확인한 경우에만 기존 evidence 계약을 원자적으로 갱신함. 성공·실패·인터럽트 모두 Portal Deployment를 원래 replica 수로 복구함.

## 3. 대상 및 경계

| 구분 | 값 | 처리 |
|---|---|---|
| Namespace | `personal-server` | 고정 대상 |
| Deployment | `portal-web` | 백업 동안 0 replica로 축소 후 원래 값으로 복구 |
| 파일 PVC | `portal-web-files-dynamic` | reader Pod에 `/data/files`로 read-only mount |
| 상태 PVC | `portal-web-state-dynamic` | reader Pod에 `/data/portal-web-state`로 read-only mount |
| 원격 저장소 | `gdrive:PersonalServer-encrypted-backups` | 암호문 artifact만 업로드 |
| 암호화 | age recipient 및 로컬 identity | 개인키·rclone token·비밀값은 Git 및 로그에 기록하지 않음 |
| 증적 | `.portal-backup-verified` | 기존 validator 계약을 사용하며 성공 때만 원자 교체 |

`portal-runtime.mode` 값이 정확히 `k3s`일 때만 새 도구를 허용함. `compose`, `cutover`, 값 누락·알 수 없는 값은 fail-closed로 중단함. 기존 `portal-backup-verify.sh`는 K3s/cutover 상태에서 계속 차단 상태로 유지함.

새 evidence에는 `source_runtime=k3s-pvc`를 필수 키로 추가함. 기존 local source evidence와 같은 `source_digest`가 우연히 일치해도 PVC backup으로 재사용하지 않도록 validator가 source runtime을 구분함.

## 4. 선택한 방식과 대안

| 방식 | 결과 | 선택 여부 |
|---|---|---|
| Deployment 중지 후 PVC reader Pod 백업 | 단일 writer를 보장하고 SQLite·파일 변경 경쟁을 제거함. 짧은 접근 불가 시간 발생 | 선택 |
| 실행 중 PVC 복사 | 파일과 SQLite가 변경 중일 수 있어 원자적 snapshot을 보장하지 못함 | 제외 |
| CSI VolumeSnapshot 도입 | local-path 환경에 별도 드라이버·운영 경계가 추가됨 | 제외 |

## 5. 실행 흐름

1. `--go` 없이 실행하면 검사만 수행하고 어떠한 Kubernetes 리소스나 Portal 상태도 변경하지 않음.
2. K3s API 접근, runtime marker, Deployment 1개, 두 PVC Bound, age/rclone/sqlite3 도구, 백업 키 권한, Google Drive remote 접근을 확인함.
3. 현재 Deployment replica 수를 읽고, 0이 아닌 값만 허용함. 0이면 현재 writer가 없으므로 실패 처리함.
4. Deployment를 0 replica로 축소하고 Portal Pod가 완전히 사라질 때까지 제한 시간 내 대기함.
5. 고유 이름의 임시 reader Pod를 만들고 두 PVC를 read-only로 mount함. Pod Ready 후 파일·상태 디렉터리와 `homeops.sqlite3` 존재를 확인함.
6. reader Pod에서 stage 영역으로 tar stream을 전송함. 실제 PVC 경로는 artifact·evidence·로그에 기록하지 않음.
7. stage의 파일 tree digest 및 상태 tree digest를 산출하고, SQLite `PRAGMA quick_check`를 실행함.
8. 기존 age 암호화, Drive immutable upload, 원격 download, 복호화, tree digest·SQLite·필수 경로 복원 검증을 수행함.
9. stage digest와 현재 유효한 `source_runtime=k3s-pvc` evidence의 `source_digest`가 같으면 암호문 재업로드 대신 기존 증적을 재사용하고 `backup_upload=SKIPPED_UNCHANGED`를 출력함. evidence 만료 전에도 writer 중지·stage·SQLite 검사는 수행하여 현재 PVC 원본과 대조함.
10. 새 archive가 필요한 경우에만 age 암호화, Drive upload/download, 별도 복원 검증을 수행함. 모든 검증 성공 시에만 evidence를 `source_runtime=k3s-pvc`, `source_digest` 포함 형식의 0600 임시 파일로 기록한 뒤 원자 교체함.
11. 임시 reader Pod와 작업 디렉터리를 제거하고 Deployment를 원래 replica 수로 복구함. Pod Ready 및 `/health`를 확인함.

## 6. 실패 및 복구 계약

| 상황 | 처리 |
|---|---|
| 사전 조건 미통과 | Deployment 축소 전 즉시 종료하고 기존 서비스 상태를 변경하지 않음 |
| 축소 후 reader/복사/업로드/복원 검증 실패 | evidence를 무효화하고 reader Pod 제거 후 Deployment를 원래 replica 수로 복구함 |
| 인터럽트(SIGINT/SIGTERM) | trap에서 reader Pod 삭제와 Deployment replica 복구를 시도함 |
| replica 복구 또는 health 확인 실패 | `portal_pvc_backup=FAIL`과 복구 실패 단계를 출력하고 운영자 개입 필요로 종료함 |
| 원격 backup이 기존과 동일 | 현재 PVC를 새 stage로 검증한 뒤, 동일한 `source_runtime=k3s-pvc` evidence가 유효하면 암호문 업로드·원격 복원만 생략함 |

증적 실패는 새 backup을 성공으로 표시하지 않는 것이 원칙임. 기존 원격 archive는 삭제하거나 덮어쓰지 않음. 복구 동작은 Deployment replica 복구만 수행하며 Caddy upstream, Cloudflare Tunnel, Compose Portal을 전환하지 않음.

## 7. 인터페이스와 출력

신규 도구 이름은 `infra/k8s/tools/portal-pvc-backup-verify.sh`로 함.

| 호출 | 동작 |
|---|---|
| `portal-pvc-backup-verify.sh --check` | 읽기 전용 사전 조건 검사. Deployment scale·Pod 생성·백업 미수행 |
| `portal-pvc-backup-verify.sh --go` | 승인된 유지보수 창에서 실제 백업·복원 검증 수행 |

성공 출력은 `portal_pvc_backup=PASS` 한 줄을 포함함. 실패 출력은 `portal_pvc_backup=FAIL`과 비밀값·경로·토큰을 포함하지 않는 단계명만 출력함. 백업 artifact와 evidence 값은 로그에 출력하지 않음.

## 8. 검증 계획

| 검증 항목 | 성공 기준 |
|---|---|
| 정적 계약 | runtime marker, fixed namespace/PVC, read-only mount, replica 복구 경로가 테스트로 확인됨 |
| 정상 경로 | reader Pod가 두 PVC 데이터를 stage로 전달하고 증적이 성공 형식으로 생성됨 |
| 사전 조건 실패 | K3s mode가 아니거나 PVC Bound가 아니면 scale 없이 실패함 |
| 복사/업로드 실패 | reader Pod 정리와 원래 replica 복구가 수행되며 성공 증적을 남기지 않음 |
| 인터럽트 | 정리·replica 복구 trap 계약을 테스트함 |
| 실제 운영 검증 | `--go` 후 Deployment Ready, Portal `/health`, evidence validator, Drive 복원본 manifest·SQLite 검사가 모두 성공함 |

## 9. 확인 필요 사항

- 백업 중 Portal 접근 불가 시간은 파일 크기·Drive 전송 속도에 따라 달라짐. 최초 운영 실행은 사용자가 관찰 가능한 유지보수 창에서 수행 필요.
- local-path PVC reader Pod는 동일 N100 node에서만 실행되어야 함. 단일 노드 K3s 조건을 사전 검사로 고정 필요.
- runtime marker가 `k3s`인 동안에는 기존 로컬 backup script를 실행하지 않음.

## 10. 후속 조치

1. 본 설계 검토 후 구현 계획 작성.
2. 테스트 우선으로 신규 도구와 회귀 테스트를 구현.
3. PR CI·독립 검토 후 사용자 승인으로 병합.
4. N100에서 `--check` 실행 후, 사용자가 선택한 유지보수 창에 `--go`를 한 번 실행.
