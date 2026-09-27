# PVC 백업 Job 종료 실패 Relay 알림 연계

## 1. 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 문서명 | PVC 백업 Job 종료 실패 Relay 알림 연계 | A1 백업 감시 보완 |
| 작성일 | 2026-09-27 | Asia/Seoul |
| 기준 자료 | 기존 Portal Job 메타데이터 읽기 전용 조회, 세 백업 CronJob manifest, `sre-telegram-relay/app/main.py` | 실제 N100 연동 전 검증 필요 |
| 목적 | 백업 runner가 상태 ConfigMap의 잠금을 게시하기 전에 종료한 실패를 Job 상태로 감지함 | 기존 상태 기반 알림 보완 |

## 2. 핵심 요약

Relay는 Book Memo·YouTube Memo·News Hub 뉴스 수집기용 고정 CronJob 세 개의 현재 UID를 읽고, 같은 namespace의 Job 중 해당 CronJob을 controller ownerReference로 가리키는 항목만 검사함. Job에 최종 `Failed=True` 조건이 있을 때만 사용자 대상 실패 알림을 전송함. Job 이름·UID·원시 실패 사유·Secret·내부 주소는 Telegram에 포함하지 않음. 세 CronJob이 아직 없으면 알림 대상으로 추정하지 않음.

## 3. 상세 계약

| 항목 | 기준 | 결과·제한 |
|---|---|---|
| 고정 대상 | `book-pvc-backup`, `youtube-pvc-backup`, `crawler-pvc-backup`의 `get` 결과에서 namespace·kind·이름·UID를 확인함 | 임의 서비스·CronJob 입력 없음 |
| Job 소유권 | `personal-server` Job의 유효 UID, 해당 CronJob 이름 접두사, 단일 controller ownerReference의 kind·name·UID를 현재 CronJob과 대조함 | 이름만 비슷한 Job, 과거 CronJob UID, owner 없는 수동 Job은 무시함 |
| 실패 판정 | Job의 `Failed=True` 최종 조건이 있고 `Complete=True` 조건은 없음 | 일시적인 `status.failed>0`·완료 Job은 무시함 |
| 전달·중복 방지 | Job UID와 서비스·실패 상태를 기존 Relay 상태 ConfigMap의 `pvc_backup_job_failed_ids` key에 저장함 | 전송 성공 뒤 기록함. 기록 실패 시 다음 polling에서 중복 전송될 수 있음 |
| 알림 문구 | 고정 서비스 대상·백업 작업 실패·복구 가능성 미확인 | Job 메타데이터와 오류 본문은 전송하지 않음 |
| 조회 오류 | 404인 미설치 CronJob은 건너뜀. 설치된 CronJob의 형식 오류와 그 밖의 API·전달 상태 오류는 Relay health 실패와 재시도로 처리함 | Job 조회가 불가능하면 알림 성공으로 간주하지 않음 |

`pvc-backup-job-reader.yaml`은 Relay ServiceAccount에 세 CronJob의 이름 지정 `get`과 `personal-server` namespace Job `list`만 부여함. CronJob 생성 Job은 이름이 실행마다 달라 `resourceNames`로 Job 목록 권한을 좁힐 수 없음. 이 Role은 같은 namespace의 다른 Job 메타데이터·상태도 읽을 수 있으므로 Relay 코드는 UID/ownerReference로 알림 대상을 다시 제한함. Pod·Pod log·Secret의 읽기와 모든 쓰기 권한은 부여하지 않음.

기존 상태 ConfigMap은 key 단위 merge patch로만 갱신함. `base.yaml` 또는 상태 ConfigMap을 재적용하면 기존 전달 기록이 초기화될 수 있으므로 이 변경의 운영 적용에 사용하지 않음. 기존 Portal 알림과 세 백업 상태 ConfigMap의 알림 기록 key는 보존함.

## 4. 검토 결과와 운영 적용 경계

2026-09-27 N100 읽기 전용 조회에서 기존 Portal 정기 백업 Job은 `CronJob/portal-pvc-backup` ownerReference, 일반 Job 이름·controller UID 라벨, 최종 `Complete=True` 조건을 가졌음. 서비스 구분용 라벨은 없었으므로 이름·라벨만으로 세 새 백업 Job을 식별하지 않음. 이 조회는 새 세 CronJob의 실제 Job 실패 전달을 검증한 결과가 아님.

| 순서 | 운영 작업 | 선행·보존 조건 |
|---:|---|---|
| 1 | 승인된 배포 전 세 CronJob·Job·Relay 현재 UID, image, 상태 key와 외부 health 읽기 전용 확인 | 세 서비스의 전용 runtime Secret·원격 ACL·수동 백업/복원 상태도 확인 필요 |
| 2 | `pvc-backup-job-reader.yaml`의 Role·RoleBinding만 적용하고 Relay SA의 고정 CronJob `get`, Job `list` 권한 확인 | Relay `base.yaml`, 상태 CM·Secret 재적용 금지 |
| 3 | 승인된 불변 digest 이미지 반입 후 A8 Relay 갱신 도구의 check/apply·rollout/rollback 검증 | 실제 N100 적용은 별도 운영 승인 범위 |
| 4 | 허용된 Job 실패와 무관 Job의 무알림, 서비스별 수신·재시작 후 중복 방지, Portal과 대상 서비스 외부 health 확인 | 백업 CronJob 자동 활성화는 Secret·원격 ACL·수동 복원·실제 알림 검증 뒤 별도 판단 |

이 PR에서 N100 동기화·RBAC 적용·이미지 반입·Relay 갱신·수동 Job 실행·CronJob 자동 활성화는 수행하지 않음.

## 5. 확인 필요 사항

- Job TTL은 각 CronJob에서 1일이고 실패 이력은 1건으로 제한됨. Relay가 그 기간보다 오래 중단되거나 Job이 삭제되면 실패를 소급 감지할 수 없음. Job 실패를 별도 운영 감시에서 확인할 필요가 있음.
- ownerReference가 없는 수동 Job과 스케줄 누락은 이 감시의 대상이 아님. Pod 시작 전 또는 scratch 초기화·상태 CM 접근 단계에서 실패하더라도 **CronJob 소유 Job에 최종 `Failed=True`가 기록된 경우에만** 감지함. 최종 실패 조건이 기록되지 않으면 별도 확인 필요.
- runner가 이미 상태 CM에 `failed`를 게시하고 Job도 최종 실패하면 기존 상태 알림과 Job 알림이 각각 전송될 수 있음. 두 신호를 안전하게 같은 실행으로 연결할 불변 식별자가 현재 상태 스키마에 없으므로 임의 시각 비교로 하나를 억제하지 않음.
- 실제 N100의 세 CronJob UID·Job ownerReference, Job 상태 실패 시점, Telegram 수신·재시작 후 전달 기록은 운영 승인 뒤 검증 필요.
- 세 전용 runtime Secret과 원격 저장소 공유 ACL은 현재 확인되지 않았으므로 수동 백업 성공·복원 검증 전 자동 실행 금지.

## 6. 후속 조치

1. PR CI·독립 검토 및 병합 뒤에도 N100 운영 적용은 보류함.
2. 세 서비스별 Secret·원격 접근과 수동 백업/복원·실제 실패 알림을 검증할 운영 계획을 마련함.
3. Job 실패 감시와 기존 상태 기반 알림을 확인한 뒤 CronJob 자동 실행 여부를 서비스별로 결정함.
