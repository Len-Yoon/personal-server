# Crawler PVC 백업·복원 검증 설계 및 결과

## 1. 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 문서명 | Crawler PVC 백업·복원 검증 설계 및 결과 | A1 세 번째 서비스 |
| 작성일 | 2026-09-27 | Asia/Seoul 기준 |
| 기준 자료 | `infra/k8s/apps/crawler-worker.yaml`, Crawler cutover 검증, Book 백업 설계, `docs/20260921_프로젝트보완_개발계획.md` | 저장소 코드 기준 |
| 목적 | Crawler PVC의 암호화 원격 백업과 격리 복원 검증 경계를 명시함 | 운영 적용 전 |

## 2. 핵심 요약

`crawler-worker-data` PVC 전용 runner와 **중지 상태** CronJob manifest를 추가함. runner는 K3s writer를 한시 중지해 전체 PVC tar snapshot과 원본 JSON·선택적 SQLite 검사를 완료한 즉시 writer를 복구함. 이후 원격 암호문 업로드·재다운로드와 격리 복원을 검증하고, 평문 scratch 삭제까지 성공한 뒤에만 성공 증적을 게시함. Portal·Book·YouTube 실행 경로는 변경하지 않음.

## 3. 상세 내용

| 구분 | 계약 | 비고 |
|---|---|---|
| 대상 | `personal-server`의 `crawler-worker` Deployment 및 `crawler-worker-data` PVC | 다른 서비스 선택 불가 |
| 원본 | writer Pod 완전 종료 후 일반 파일·디렉터리 전체 | symlink·특수 파일·hardlink 거절, PVC read-only mount |
| 상태 검증 | `news_archive.json`의 v3 schema·기사 목록·outbox 목록, `news_collection_status.json`의 JSON 객체 | 앱의 archive loader는 이전 버전 migration/write 가능성이 있어 호출하지 않음 |
| 사전 검사 | `--check`는 JSON·SQLite·Secret·원격 접근을 읽기 전용 검사함. `--go`는 잠금 취득과 이전 증적 무효화 후 동일 검사를 수행함 | 실행 중 사전 검사 실패도 `failed` 상태로 기록함 |
| SQLite | PVC에 존재하는 `.sqlite`·`.sqlite3` 파일마다 `PRAGMA quick_check` | `news_summaries.sqlite3`을 필수 파일로 가정하지 않음 |
| 복원 무결성 | 원본·격리 복원 트리 SHA-256, 다운로드 암호문 SHA-256, 양쪽 JSON·SQLite 검사 | 운영 PVC에 복원하지 않음 |
| 암호화·원격 | age, 고정 `crawler-worker` 원격 prefix, `rclone copyto --immutable` | 자격 증명은 Secret 파일만 참조 |
| 중복 실행 | CronJob `Forbid`와 단일 상태 ConfigMap의 원자적 잠금 | 상태 ConfigMap은 별도 bootstrap-only manifest로 최초 생성하고 재적용 금지 |
| 실패 처리 | pause 1→0·resume 0→1 모두 Deployment UID 재확인과 `resourceVersion`·현재 replica 사전조건으로 scale함. pause 성공 후 실패는 조건부 writer 복구를 우선하고 평문 scratch 삭제 성공 후 상태 patch | 동시 교체·replica 변경, pause 응답 불명확, 복구·삭제 실패 시 잠금 유지. pause 결과가 불명확하면 writer 상태 수동 확인·복구 필요 |
| 서비스 영향 | tar snapshot·원본 검사부터 writer readiness 회복까지 News Hub HTTP와 수집 scheduler가 중단될 수 있음. 암호화·원격 검증은 복구 뒤 진행함 | 실제 중지 시간·외부 감시 알림 영향 확인 필요 |
| 자동 실행 | `suspend: true`, 미설정 image sentinel | 실패 Relay 연결·수동 운영 검증 전 활성화 차단 |

## 4. 검토 결과

| 항목 | 결과 | 비고 |
|---|---|---|
| 로컬 모의 검증 | 전체 tar·암호문 업로드/다운로드·격리 복원, JSON·선택적 SQLite·해시 확인, writer 복구와 잠금 실패 경계 | 실제 K3s·원격 저장소 검증 아님 |
| 복원 증적 범위 | `restore_status=success`는 파일 트리 해시, JSON 구조, 존재하는 SQLite의 `quick_check` 성공을 뜻함 | mode·UID/GID·ACL·xattr, 앱 UID 쓰기, JSON의 업무상 의미, SQLite WAL의 논리 일관성은 미검증 |
| 자격 증명 | `crawler-worker-pvc-backup-runtime`의 `rclone-config`, `rclone-config-passphrase`, `age-recipient`, `age-identity` key 이름만 manifest에 포함 | 값 생성·저장·출력 없음 |
| 실패 알림 | 상태 ConfigMap에 실패 단계·잠금을 기록함 | SRE Relay 연결은 미구현 |

## 5. 확인 필요 사항

| 항목 | 확인 필요 내용 |
|---|---|
| 실제 운영 상태 | 2026-09-27 N100 읽기 전용 확인에서 Deployment 1/1·PVC Bound 1Gi 확인됨. PVC UID·단일 writer 전체 경계·현재 파일 구성·크기·원격 접근은 추가 확인 필요 |
| 운영 Secret | Crawler 전용 백업 runtime Secret 부재 확인됨. 값은 조회하지 않았으며 승인된 암호화 저장소를 이용한 시딩 방식은 최종 운영 승인에서 결정 필요. 준비 전 수동 Job·자동 실행 모두 차단 |
| 리소스 규모 | PVC 실제 크기, scratch 10Gi 한도, 중지 시간, 예약 시각 13:00 KST 적정성 확인 필요 |
| 복구 경계 | SIGKILL·노드 장애로 cleanup이 중단되면 writer와 잠금 상태의 수동 복구 절차 확인 필요 |
| Job 실패 감시 | 상태 잠금 게시 전 scratch 초기화 또는 ConfigMap 접근이 실패하면 Relay가 `failed` 상태를 감지할 수 없음. Kubernetes Job 실패를 별도로 감시할 필요 |
| 알림 | Crawler 실패 상태의 Relay 전송과 실제 수신을 검증하기 전 자동 실행 활성화 금지 |

## 6. 후속 조치

1. 독립 보안·운영 검토에서 RBAC, 잠금, 종료·교체 경계와 Secret 계약을 확인함.
2. 별도 N100 운영 승인 후 runtime marker·단일 writer·PVC UID·외부 health를 읽기 전용 확인하고 이미지 digest를 준비함.
3. Secret·원격 접근 준비 뒤 상태 ConfigMap은 존재하지 않을 때 한 번만 생성하고 중지 CronJob을 적용함. 수동 Job의 암호문·격리 복원·Crawler 및 Portal 외부 health를 확인함.
4. Relay 실패 알림을 구현·검증한 뒤 자동 실행 활성화 승인을 확인함.

저장소 병합=미수행 / N100 동기화=미수행 / 이미지 반입=미수행 / 수동 운영 검증=미수행 / 자동 실행 활성화=미수행 / 외부 health=미수행.
