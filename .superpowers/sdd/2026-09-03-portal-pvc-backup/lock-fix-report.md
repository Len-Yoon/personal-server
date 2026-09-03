# Portal PVC backup lock corrective report

## Status

- 상태: 완료
- 범위: 로컬 비블로킹 `flock` lock 전환 및 health fake 검증 보강
- 제외: Kubernetes/N100 실환경, rclone, Docker, Caddy/Tunnel/Compose/startup/scheduler

## 변경 내용

- Kubernetes Lease 생성·조회·삭제를 제거함.
- `PORTAL_BACKUP_LOCK_FILE` 경로의 파일을 FD 9로 열고 `flock -n`으로 스크립트 수명 동안 점유함.
- 경합 시 lock 단계 FAIL을 출력하고 scale/reader Pod 생성 없이 종료함.
- `--check`는 lock 획득 없이 읽기 전용으로 유지함.
- cleanup에서 FD를 닫아 lock을 해제함.
- fake kubectl exec가 정확한 urllib health probe만 허용하고, 비-200 응답을 실패로 시뮬레이션하도록 보강함.

## 검증

- `python3 -m unittest tests/test_k8s_portal_pvc_backup_verify.py`: 22개 통과
- `bash -n infra/k8s/tools/portal-pvc-backup-verify.sh`: 통과
- `git diff --check`: 통과
- 변경 하네스 `maintenance=success`: `ready_for_review`

## Concerns

- 실제 N100/Kubernetes에서 실행하지 않음. 운영 적용 전 `flock` 명령 제공 여부와 lock 파일 경로 권한 확인 필요.
