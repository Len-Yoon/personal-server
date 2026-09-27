# YouTube Memo PVC 백업·복원 검증 설계 및 결과

## 1. 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 문서명 | YouTube Memo PVC 백업·복원 검증 설계 및 결과 | A1 두 번째 서비스 |
| 작성일 | 2026-09-27 | Asia/Seoul 기준 |
| 기준 자료 | `infra/k8s/apps/youtube-memo.yaml`, Book·Portal 백업 도구, `docs/20260921_프로젝트보완_개발계획.md` | 저장소 코드 기준 |
| 목적 | YouTube Memo의 PVC를 암호화하여 원격 보관하고 격리 복원으로 검증하는 개발 범위 명시 | 운영 적용 전 |

## 2. 핵심 요약

YouTube Memo 단일 PVC `youtube-memo-data`용 백업 runner와 **중지 상태** CronJob manifest를 추가함. runner는 K3s YouTube writer를 한시적으로 중지해 전체 PVC의 tar snapshot과 원본 SQLite 검사를 완료한 즉시 writer를 복구함. 이후 age 암호화, 원격 업로드·재다운로드, 격리 복원·SHA-256·SQLite 검증을 수행함. 검증·writer 재가동·평문 scratch 삭제가 모두 성공한 뒤에만 성공 증적을 기록함. Portal 백업 실행 경로는 변경하지 않음.

## 3. 상세 내용

| 구분 | 계약 | 비고 |
|---|---|---|
| 대상 | `personal-server`의 `youtube-memo` Deployment 및 `youtube-memo-data` PVC | 다른 서비스 선택 불가 |
| 백업 원본 | writer Pod 완전 종료 후 PVC의 일반 파일·디렉터리 전체 | symlink·특수 파일 거절, PVC 읽기 전용 mount |
| 무결성 | 원본·격리 복원 트리 SHA-256, 암호문 다운로드 SHA-256, 양쪽 `PRAGMA quick_check` | 운영 PVC에 복원하지 않음 |
| 사전 검사 | `--check`는 SQLite·Secret·원격 접근을 읽기 전용 검사함. `--go`는 잠금 취득과 이전 증적 무효화 후 동일 검사를 수행함 | 실행 중 사전 검사 실패도 `failed` 상태로 기록함 |
| 암호화·원격 | age, 고정 `youtube-memo` 원격 prefix, `rclone copyto --immutable` | 자격 증명은 Secret 파일만 참조 |
| 중복 실행 | CronJob `Forbid`와 단일 상태 ConfigMap의 원자적 잠금 | 상태 ConfigMap은 별도 bootstrap-only manifest로 최초 생성하고 이후 재적용 금지. 잠금 잔류 시 자동 탈취하지 않음 |
| 증적 순서 | 잠금 취득과 동시에 이전 성공 증적을 비움. 평문 scratch 삭제 성공 뒤 성공 증적·완료 상태·잠금 해제를 한 번의 원자적 patch로 처리함 | scratch 삭제 실패 시 실행 중 잠금과 빈 증적을 유지하고 수동 확인 필요. 게시 실패 시 이전 성공 증적이 되살아나지 않음 |
| 실패 처리 | pause 1→0·resume 0→1 모두 Deployment UID를 재확인하고 `resourceVersion`·현재 replica 사전조건으로 scale함. pause 성공 후 실패는 조건부 writer 복구를 우선하며 평문 scratch 삭제 성공 뒤 실패 상태를 게시함 | 동시 Deployment 교체·replica 변경, pause 응답 불명확, 복구 실패 또는 scratch 삭제 실패 시 잠금을 유지해 자동 재실행 차단. pause 결과가 불명확하면 writer 상태를 수동 확인·복구해야 함 |
| 서비스 영향 | tar snapshot·원본 검사 동안 YouTube Memo HTTP가 한시 중단됨. 암호화·원격 전송·격리 복원은 writer 복구 뒤 진행함 | 실제 중지 시간·외부 감시 알림 영향 확인 필요 |
| 자동 실행 | manifest의 `suspend: true`, 미설정 image sentinel | 운영 승인 전 활성화하지 않음 |

## 4. 검토 결과

| 항목 | 결과 | 비고 |
|---|---|---|
| 로컬 모의 검증 | 전체 tar 생성·격리 복원, SQLite·해시 확인, 원격 복원 실패 시 writer 재가동, symlink·경로 이탈 거절 | 실제 K3s·원격 저장소 검증은 아님 |
| 복원 증적 범위 | `restore_status=success`는 격리 복원의 파일 트리 해시와 SQLite `quick_check` 성공을 뜻함 | 파일 mode·UID/GID·ACL·xattr, 앱 UID의 쓰기 가능성, WAL의 논리 일관성은 검증하지 않음 |
| 자격 증명 | `youtube-memo-pvc-backup-runtime`의 `rclone-config`, `rclone-config-passphrase`, `age-recipient`, `age-identity` key 이름만 manifest에 기재함 | 값 생성·저장·출력 없음 |
| 실패 알림 | YouTube 상태 ConfigMap에 증적·상태·잠금을 함께 기록함. 실패·실행 중에는 기존 성공 증적을 비움 | 기존 SRE relay는 Portal 백업 전용이며 YouTube 알림 연결은 미구현 |
| 기존 Portal | Portal script·CronJob·validator·Relay 실행 경로 변경 없음 | 운영 중 백업과 분리 |

## 5. 확인 필요 사항

| 항목 | 확인 필요 내용 |
|---|---|
| 실제 운영 상태 | 2026-09-27 N100 읽기 전용 확인에서 Deployment 1/1·PVC Bound 1Gi 확인됨. PVC UID·파일 구성·single writer 전체 경계·원격 접근은 추가 확인 필요 |
| 운영 Secret | YouTube 전용 백업 runtime Secret 부재 확인됨. 값은 조회하지 않았으며 승인된 암호화 저장소를 이용한 시딩 방식은 최종 운영 승인에서 결정 필요. 준비 전 수동 Job·자동 실행 모두 차단 |
| 원격 공유 권한 | 백업 대상 원격 저장소의 공유·접근 권한 확인 필요 |
| 운영 규모 | PVC 실제 크기, scratch 10Gi 한도, 중지 시간, `auth-rate-limit-state.json` 등 `youtube_memo.sqlite3` 이외 파일의 실제 구성 확인 필요 |
| 복구 경계 | SIGKILL·노드 장애처럼 cleanup이 실행되지 못할 경우 YouTube writer 수동 복구 절차 확인 필요 |
| Job 실패 감시 | 상태 잠금 게시 전 scratch 초기화 또는 ConfigMap 접근이 실패하면 Relay가 `failed` 상태를 감지할 수 없음. Kubernetes Job 실패를 별도로 감시할 필요 |
| 알림 | YouTube 실패 상태를 SRE relay에 연결하고 실제 전송을 검증하기 전에는 자동 실행 활성화 차단 필요 |

## 6. 후속 조치

1. 독립 보안·운영 검토에서 RBAC, 잠금, 종료 경계, Secret 계약을 확인함.
2. 별도 N100 운영 승인 후 기존 runtime marker·YouTube 단일 writer·PVC UID·외부 health를 읽기 전용으로 확인하고, 실제 이미지 digest를 준비함.
3. Secret과 원격 접근을 준비한 뒤 상태 ConfigMap bootstrap manifest는 존재하지 않을 때 한 번만 생성하고, 중지 CronJob manifest를 별도 적용함. 수동 Job 1회를 실행하여 원격 암호문·복원 증적·YouTube 및 Portal 외부 health를 확인함. 상태 ConfigMap을 기존 증적 위에 재적용하지 않음.
4. YouTube 실패 알림 경로를 구현·검증한 뒤 자동 실행 활성화 승인을 확인함.

저장소 병합=미수행 / N100 동기화=미수행 / 이미지 반입=미수행 / 수동 운영 검증=미수행 / 자동 실행 활성화=미수행 / 외부 health=미수행.
