# Portal PVC Backup Release Fix 보고서

## 작업 결과

- Portal 복구 후 health 확인을 `wget`에서 Python `urllib.request` 기반 호출로 변경함.
- health 응답 상태가 HTTP 200인 경우에만 성공 처리함.
- Lease 생성 실패 또는 결과 불명확 시 Lease를 read-back함.
- read-back한 `.spec.holderIdentity`가 현재 실행의 holder와 정확히 일치하는 경우에만 해당 Lease를 삭제함.
- 기존 read-only check, 증적 생성 시점, 진단 로그 비공개 및 출력 redaction 동작은 유지함.

## TDD 결과

### RED

두 회귀 테스트를 먼저 추가하고 실행함.

```text
Ran 2 tests in 2.619s
FAILED (failures=2)
```

- `test_restored_portal_health_uses_urllib_and_requires_http_200`: 기존 `wget` health probe로 인해 실패함.
- `test_failed_lease_create_reads_back_and_deletes_only_matching_holder`: Lease 생성 실패 후 read-back이 없어 실패함.

### GREEN

수정 후 신규 회귀 테스트를 실행함.

```text
Ran 2 tests in 3.585s
OK
```

## 검증 결과

| 검증 항목 | 결과 |
|---|---|
| `python3 -m unittest tests/test_k8s_portal_pvc_backup_verify.py` | 성공, 22개 테스트 |
| `bash -n infra/k8s/tools/portal-pvc-backup-verify.sh` | 성공 |
| `git diff --check` | 성공 |
| 변경 범위 하네스 (`maintenance=success`) | `ready_for_review` |

## 변경 파일

- `infra/k8s/tools/portal-pvc-backup-verify.sh`
- `tests/test_k8s_portal_pvc_backup_verify.py`

## 커밋

- SHA: 6b3d0af

## 확인 필요 사항 및 잔여 위험

- 실제 클러스터, live `kubectl`, rclone, Docker 환경에서는 검증하지 않음.
- Python urllib 호출의 네트워크 동작은 테스트 fake 환경에서 HTTP 200 분기만 검증함.
