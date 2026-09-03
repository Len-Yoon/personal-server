# Portal K3s PVC 백업 최종 수정 보고서

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | Portal K3s PVC 백업 최종 수정 보고서 |
| 작성일 | 2026-09-03 |
| 기준 자료 | 최종 리뷰 `dc00b94..edf96d2`, 설계서·구현계획·진행 원장 |
| 대상 브랜치 | `codex/portal-pvc-backup-design` |
| 커밋 | `09ef583` |
| 운영 실행 | 수행하지 않음. N100/Docker/kubectl/rclone 실환경 미사용 |

## 2. 핵심 요약

최종 리뷰의 4개 범주를 단일 수정 웨이브로 반영함.

- Portal 원래 replica 복구 후 Deployment rollout과 Pod 내부 `/health`가 모두 성공한 경우에만 evidence를 원자 교체하고 PASS를 출력하도록 변경함.
- `--go`에 `personal-server/portal-pvc-backup-lock` 고정 Kubernetes Lease를 적용하고, 경합 시 scale·reader Pod 생성을 차단하며 cleanup에서 Lease를 삭제하도록 변경함. `--check`는 Lease를 생성하지 않음.
- rclone 원격 read-only preflight를 scale 전에 수행하고, tar/age/rclone 및 Kubernetes 오류는 0600 진단 파일로만 수집하며 고정 stage label만 출력하도록 변경함.
- Compose 성공 신규 evidence의 `source_runtime=compose-local` 직접 회귀 테스트와 동적 timestamp cutover fixture를 추가함. 운영 문서에 K3s `--check` → 사용자 유지보수 창 `--go` 순서를 명시함.

## 3. RED / GREEN 검증

### 3.1 RED

최종 리뷰 지적을 재현하는 테스트를 먼저 추가한 뒤 기존 구현에서 실패 확인함.

| 항목 | RED 근거 |
|---|---|
| 복구 후 readiness/health | 기존 성공 경로에 rollout·Pod health 호출이 없어 신규 assertion 실패함 |
| Lease 경합 | 기존 구현이 lock 없이 진행하여 경합 fixture가 0 replica scale 경로로 진행함 |
| 원격 preflight | 기존 구현이 rclone 원격 접근 확인 없이 scale을 수행함 |
| noisy 오류 | 기존 tar/age/rclone stderr가 operator 출력으로 전파될 수 있는 구조였음 |
| Compose evidence | 신규 성공 fixture가 portable expiry 처리 전 `date -d` 오류로 실패함 |

### 3.2 GREEN

| 검증 | 결과 |
|---|---|
| `python3 -m unittest tests.test_validate_backup_evidence tests.test_k8s_portal_backup_verify tests.test_k8s_portal_cutover` | 77건 통과 |
| `python3 -m unittest tests.test_k8s_portal_pvc_backup_verify` | 20건 통과 |
| `bash -n infra/k8s/tools/portal-backup-verify.sh infra/k8s/tools/portal-pvc-backup-verify.sh infra/k8s/tools/portal-cutover.sh` | 통과 |
| `git diff --check` | 통과 |
| `run_change_harness.py ... --check-result maintenance=success --agent-context` | `ready_for_review` |
| 실환경 실행 | 수행하지 않음. 요구사항에 따라 N100/Docker/kubectl/rclone 미사용 |

신규 회귀 테스트는 rollout 실패·health 실패 시 evidence 삭제와 FAIL, Lease 경합 시 scale/reader 미실행, 원격 preflight 실패 시 무변경 FAIL 및 noisy path redaction, Compose 신규 evidence의 runtime marker 기록을 직접 검증함.

## 4. 변경 파일

| 파일 | 변경 내용 |
|---|---|
| `infra/k8s/tools/portal-pvc-backup-verify.sh` | Lease lock, 원격 preflight, private diagnostics, 복구 후 rollout/Pod health gate, evidence 지연 기록 |
| `infra/k8s/tools/portal-backup-verify.sh` | Compose evidence 성공 fixture를 지원하는 portable expiry 계산 |
| `tests/test_k8s_portal_pvc_backup_verify.py` | readiness/health, lock, remote preflight, 오류 redaction fake-command 회귀 테스트 |
| `tests/test_k8s_portal_backup_verify.py` | Compose 성공 신규 evidence의 `source_runtime=compose-local` 직접 검증 및 fake rclone 성공 경로 |
| `tests/test_k8s_portal_cutover.py` | cutover evidence timestamp를 현재 기준 동적 생성 |
| `docs/k3s-flux-transition-draft.md` | Compose/K3s 도구 구분 및 K3s `--check` → 유지보수 창 `--go` 운영 절차 |
| `.superpowers/sdd/2026-09-03-portal-pvc-backup/final-fix-report.md` | 본 최종 수정 보고서 |

서버 기동 스크립트, 스케줄러, CI, Caddy, Tunnel, Compose 설정은 변경하지 않음.

## 5. 커밋

최종 커밋: `09ef583`

커밋 메시지: `fix: Portal PVC 백업 최종 안전성 보강`

## 6. 확인 필요 사항 및 잔여 위험

- 실제 N100 `--check` 및 승인된 유지보수 창의 `--go` 실행은 사용자 승인 후 별도 수행 필요함.
- 실제 클러스터에서 Lease RBAC 권한, Portal health endpoint(포트 8000), rclone remote 권한은 운영 실행 시 확인 필요함.
- 본 검증은 fake command 기반이며 실제 PVC·K3s rollout·Google Drive 복원은 미검증 상태임.

## 7. 에이전트 운영 기록

- 주 에이전트: 최종 수정, 테스트·정적 검사·harness·diff 검증 및 보고서 작성 담당함.
- 하위 에이전트: 사용하지 않음. 상위 작업에서 단일 구현 에이전트로 최종 수정 웨이브 범위를 명시했으며, 본 작업은 별도 분업 없이 수행함.
