# Portal 백업 원격 사전점검 재시도 설계

## 1. 장애 근거

2026-09-12 03:00 실행된 `portal-pvc-backup` Job은 `remote-timeout`으로 실패함. `rclone lsd` 원격 사전점검의 30초 deadline에서 종료됐으며, Portal writer 중지 이전 단계였음. 이후 수동 Job은 `portal_pvc_backup=PASS`, `backup_upload=SKIPPED_UNCHANGED`로 완료됨.

## 2. 목표

일시적인 원격 응답 지연으로 백업이 불필요하게 실패하지 않도록, 원격 사전점검 timeout에만 제한된 재시도와 backoff를 적용함.

## 3. 범위와 제약

- `rclone lsd` 사전점검에만 적용함. upload/restore 재시도는 추가하지 않음.
- timeout(exit 124)만 재시도함. 인증·경로·설정 오류는 즉시 기존 분류로 실패함.
- 기본값은 시도당 30초, 추가 재시도 1회, backoff 5초로 제한함.
- 실패 stage와 비밀값 비노출 계약을 유지함.
- Portal writer, PVC, CronJob schedule/RBAC/Secret, Caddy 및 자동 실행 방식은 변경하지 않음.

## 4. 성공 기준

- 첫 timeout 뒤 두 번째 `lsd`가 성공하면 writer 중지 전에 백업 흐름이 진행됨.
- timeout이 모두 소진되면 `remote-timeout`으로 실패하며 writer는 중지하지 않음.
- 비timeout 원격 오류는 재시도하지 않고 기존 안전 분류를 유지함.
