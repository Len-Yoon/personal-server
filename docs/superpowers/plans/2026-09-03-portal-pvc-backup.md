# Portal K3s PVC 백업 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** K3s에서 실행 중인 Portal의 실제 PVC 데이터를 일관성 있게 암호화 백업하고 Google Drive 복원 검증 후 증적을 생성함.

**Architecture:** `portal-runtime.mode=k3s`에서만 동작하는 operator-only 도구가 Portal Deployment를 잠시 0 replica로 축소함. 임시 reader Pod가 두 PVC를 read-only mount하여 stage로 스트리밍하고, 기존 age·rclone·복원 검증 계약을 재사용함. 모든 종료 경로는 임시 Pod 정리와 원래 replica 복구를 수행하며, 증적에는 backup 원본 runtime을 명시함.

**Tech Stack:** Bash, K3s/kubectl, Kubernetes PVC·Pod, age, rclone, SQLite, Python unittest.

**Spec:** `docs/superpowers/specs/2026-09-03-portal-pvc-backup-design.md`

## Global Constraints

- `personal-server` namespace의 `portal-web`, `portal-web-files-dynamic`, `portal-web-state-dynamic`만 대상으로 함.
- `portal-runtime.mode`이 정확히 `k3s`가 아니면 fail-closed로 중단함.
- Caddy, Cloudflare Tunnel, Compose Portal, 서버 기동 스크립트, 스케줄러, 원격 archive 삭제는 변경하지 않음.
- 모든 timestamp는 UTC ISO-8601 `Z` 형식으로 기록함.
- Secret, age 개인키, rclone token, PVC host path, 암호문 artifact 경로는 출력·Git 기록·명령 인수에 포함하지 않음.
- `--check`는 읽기 전용이며 `--go`만 Deployment scale 및 임시 reader Pod 생성을 허용함.
- 실패·인터럽트 시 증적을 성공으로 남기지 않고 Portal Deployment replica를 원래 값으로 복구함.
- 실제 N100 `--go` 실행은 PR 병합 및 사용자 유지보수 창 승인 뒤에만 수행함.

---

## File Structure

| 파일 | 책임 |
|---|---|
| `infra/k8s/tools/validate-backup-evidence.py` | evidence의 허용 키·source runtime 값 검증 |
| `infra/k8s/tools/portal-backup-verify.sh` | Compose-local backup에 `source_runtime=compose-local` 기록·재사용 제한 |
| `infra/k8s/tools/portal-pvc-backup-verify.sh` | K3s PVC preflight, writer 중지, reader Pod stream, 암호화·원격 복원 검증, 원복 |
| `infra/k8s/tools/portal-cutover.sh` | Compose→K3s cutover가 compose-local evidence만 수용하도록 경계 강화 |
| `tests/test_validate_backup_evidence.py` | source runtime validator 정상·오류 계약 |
| `tests/test_k8s_portal_backup_verify.py` | Compose backup evidence source runtime 회귀 테스트 |
| `tests/test_k8s_portal_pvc_backup_verify.py` | 신규 PVC backup 도구의 fake command 정상·실패·원복 계약 |
| `tests/test_k8s_portal_cutover.py` | cutover가 잘못된 source runtime evidence를 거부하는 회귀 테스트 |
| `docs/k3s-flux-transition-draft.md` | Compose-local/K3s-PVC backup 경계와 운영 절차 |

## Agent Roles

| 역할 | 담당 범위 | 같은 파일 동시 수정 금지 |
|---|---|---|
| 주 에이전트 | 작업 경계, harness, 통합 검증·PR 준비 | 구현 파일 직접 수정하지 않음 |
| 구현 에이전트 | Task 1~3의 테스트 우선 구현 | reviewer와 파일 동시 수정 금지 |
| 독립 검토 에이전트 | diff, rollback trap, secret/log 노출, 금지 영역 검토 | 구현 변경 금지 |
| 운영·보안 전문 검토 에이전트 | Kubernetes RBAC·PVC mount·운영 되돌리기 검토 | 구현 변경 금지 |

## Task 1: Backup Evidence 원본 Runtime 계약

**Files:**
- Modify: `infra/k8s/tools/validate-backup-evidence.py`
- Modify: `infra/k8s/tools/portal-backup-verify.sh`
- Modify: `tests/test_validate_backup_evidence.py`
- Modify: `tests/test_k8s_portal_backup_verify.py`

**Interfaces:**
- Produces: `source_runtime` evidence key, allowed values `compose-local` and `k3s-pvc`.
- Consumes: 기존 `source_digest`, timestamp, encrypted/restore success evidence contract.
- Guarantees: compose-local 도구는 자신이 만든 evidence만 재사용함.

- [ ] **Step 1: source runtime이 없는 evidence를 거부하는 failing validator test 작성**

```python
def test_rejects_evidence_without_source_runtime(self):
    self.assertNotEqual(self.run_validator(valid_evidence()).returncode, 0)

def test_accepts_only_known_source_runtime_values(self):
    self.assertEqual(
        self.run_validator(valid_evidence(source_runtime="compose-local")).returncode, 0
    )
    self.assertNotEqual(
        self.run_validator(valid_evidence(source_runtime="unknown")).returncode, 0
    )
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `python3 -m unittest tests.test_validate_backup_evidence.ValidateBackupEvidenceTests -v`

Expected: 새 테스트가 source runtime 누락을 허용하는 현재 validator 때문에 실패함.

- [ ] **Step 3: validator의 required key와 값 제한 구현**

```python
REQUIRED_KEYS = frozenset({..., "source_runtime"})
SOURCE_RUNTIME_VALUES = frozenset({"compose-local", "k3s-pvc"})

if values["source_runtime"] not in SOURCE_RUNTIME_VALUES:
    raise EvidenceError("source_runtime is not approved")
```

- [ ] **Step 4: Compose backup이 compose-local marker를 기록하고 같은 marker evidence만 재사용하도록 failing regression test 작성**

```python
def test_compose_backup_does_not_reuse_k3s_pvc_evidence(self):
    result, rclone_called, _ = self._run_fake_backup(
        source_runtime="k3s-pvc"
    )
    self.assertNotEqual(result.returncode, 0)
    self.assertTrue(rclone_called)
```

- [ ] **Step 5: Compose backup 최소 구현**

`portal-backup-verify.sh`의 evidence 재사용 조건과 새 evidence 작성에 아래 값을 추가함.

```bash
COMPOSE_SOURCE_RUNTIME='compose-local'
evidence_source_runtime=$(awk -F= '$1 == "source_runtime" { print $2 }' "$EVIDENCE")
[ "$evidence_source_runtime" = "$COMPOSE_SOURCE_RUNTIME" ] || false
...
"source_runtime=$COMPOSE_SOURCE_RUNTIME"
```

- [ ] **Step 6: Task 1 관련 테스트 실행**

Run: `python3 -m unittest tests.test_validate_backup_evidence tests.test_k8s_portal_backup_verify -v`

Expected: 모든 테스트 통과.

- [ ] **Step 7: Task 1 커밋**

```bash
git add infra/k8s/tools/validate-backup-evidence.py \
  infra/k8s/tools/portal-backup-verify.sh \
  tests/test_validate_backup_evidence.py \
  tests/test_k8s_portal_backup_verify.py
git commit -m "fix: Portal 백업 원본 runtime 증적 구분"
```

## Task 2: K3s PVC 백업 도구의 실패 우선 계약

**Files:**
- Create: `infra/k8s/tools/portal-pvc-backup-verify.sh`
- Create: `tests/test_k8s_portal_pvc_backup_verify.py`

**Interfaces:**
- Consumes: `PORTAL_NAMESPACE`, `PORTAL_RUNTIME_MARKER`, `PORTAL_BACKUP_EVIDENCE`, age/rclone local configuration.
- Produces: `portal_pvc_backup=PASS|FAIL`; 성공 evidence의 `source_runtime=k3s-pvc`.
- Guarantees: `--check`에서 Kubernetes 변경 없음; `--go` 실패와 signal에서 original replicas 복구 시도.

- [ ] **Step 1: preflight와 복구 계약을 검증하는 failing fake-command tests 작성**

```python
def test_check_mode_never_scales_or_creates_reader_pod(self):
    result, calls = self.run_tool("--check", runtime="k3s")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertNotIn("scale deployment/portal-web", calls)
    self.assertNotIn("create -f", calls)

def test_go_failure_restores_original_replica_and_deletes_reader(self):
    result, calls = self.run_tool("--go", runtime="k3s", fail_at="stream")
    self.assertNotEqual(result.returncode, 0)
    self.assertIn("scale deployment/portal-web --replicas=1", calls)
    self.assertIn("delete pod", calls)
```

`run_tool`은 임시 `kubectl`, `age`, `rclone`, `sqlite3`, `timeout` fake command를 `PATH` 우선순위로 주입하고 호출 문자열만 기록함. 실제 Kubernetes·Google Drive·Docker를 사용하지 않음.

- [ ] **Step 2: 새 테스트가 도구 부재로 실패하는지 확인**

Run: `python3 -m unittest tests.test_k8s_portal_pvc_backup_verify -v`

Expected: 새 script가 없어서 실패함.

- [ ] **Step 3: 읽기 전용 preflight와 고정 대상 검증 구현**

```bash
NAMESPACE="${PORTAL_NAMESPACE:-personal-server}"
DEPLOYMENT='portal-web'
FILES_PVC='portal-web-files-dynamic'
STATE_PVC='portal-web-state-dynamic'

assert_runtime_k3s() { [ "$(<"$RUNTIME_MARKER")" = k3s ]; }
assert_bound_pvc() {
  sudo k3s kubectl -n "$NAMESPACE" get "pvc/$1" \
    -o jsonpath='{.status.phase}' | grep -Fxq Bound
}
```

`--check`는 runtime marker, node Ready 1개, Deployment replica 1개, PVC Bound 2개, 필요한 명령·키 파일·evidence directory 권한만 확인하고 `portal_pvc_backup=PASS`를 출력함.

- [ ] **Step 4: writer 중지·reader Pod·stage stream·cleanup 구현**

```bash
original_replicas=$(sudo k3s kubectl -n "$NAMESPACE" get deployment "$DEPLOYMENT" -o jsonpath='{.spec.replicas}')
sudo k3s kubectl -n "$NAMESPACE" scale "deployment/$DEPLOYMENT" --replicas=0
wait_for_portal_pods_absent
create_reader_pod_with_read_only_mounts
stream_pvc_tree "$READER_POD" /data/files "$STAGE/data/files"
stream_pvc_tree "$READER_POD" /data/portal-web-state "$STAGE/data/portal-web-state"
```

reader Pod manifest는 busybox 계열의 이미지를 사용하고 `readOnly: true`, `restartPolicy: Never`, 고유한 RFC 1123 name만 허용함. `trap cleanup EXIT INT TERM HUP`에서 reader Pod를 삭제하고 `original_replicas`가 0보다 큰 경우에만 원래 값으로 scale함. `cleanup`은 실패 시 evidence를 삭제하되, 원격 archive를 삭제하지 않음.

- [ ] **Step 5: 암호화·복원 검증과 evidence 재사용 구현**

stage 뒤에는 기존 tool과 동일한 `tree_digest`, age, immutable rclone upload, remote download, decrypt, manifest, `PRAGMA quick_check` 흐름을 사용함. 재사용은 validator를 통과한 `source_runtime=k3s-pvc` evidence와 같은 source digest인 경우에만 허용함.

```bash
if evidence_is_current_k3s_pvc "$source_digest"; then
  printf '%s\n' 'portal_pvc_backup=PASS' 'backup_upload=SKIPPED_UNCHANGED'
else
  create_upload_download_and_restore_verify
  write_evidence 'k3s-pvc' "$source_digest"
fi
```

성공·실패 어느 경우에도 `restore_portal_replicas`가 먼저 성공해야 최종 status를 출력함. 복구 실패 시 `portal_pvc_backup=FAIL`만 출력함.

- [ ] **Step 6: 정상·오류·cleanup 경계 테스트 확대**

최소 테스트 케이스:

```python
cases = {
    "compose runtime": {"runtime": "compose", "expect_scale": False},
    "missing PVC": {"missing_pvc": True, "expect_scale": False},
    "reader failure": {"fail_at": "reader", "expect_restore": True},
    "stream failure": {"fail_at": "stream", "expect_restore": True},
    "upload failure": {"fail_at": "upload", "expect_restore": True},
    "matching pvc evidence": {"source_runtime": "k3s-pvc", "expect_upload": False},
}
```

각 실패 case에서 기존 `portal-web` replica 값 복구 호출, reader delete, success evidence 미생성을 확인함. `k3s-pvc` marker가 아닌 evidence는 업로드 재사용을 허용하지 않음을 확인함.

- [ ] **Step 7: Task 2 관련 테스트와 shell 정적 검사 실행**

Run:

```bash
bash -n infra/k8s/tools/portal-pvc-backup-verify.sh
python3 -m unittest tests.test_k8s_portal_pvc_backup_verify -v
```

Expected: syntax error 없음, 모든 test 통과.

- [ ] **Step 8: Task 2 커밋**

```bash
git add infra/k8s/tools/portal-pvc-backup-verify.sh \
  tests/test_k8s_portal_pvc_backup_verify.py
git commit -m "feat: Portal PVC 암호화 백업 검증 추가"
```

## Task 3: Cutover Evidence 경계와 운영 문서 갱신

**Files:**
- Modify: `infra/k8s/tools/portal-cutover.sh`
- Modify: `tests/test_k8s_portal_cutover.py`
- Modify: `docs/k3s-flux-transition-draft.md`

**Interfaces:**
- Consumes: validator를 통과한 evidence.
- Produces: Compose→K3s cutover는 `source_runtime=compose-local` evidence만 허용함.
- Guarantees: K3s PVC evidence가 Compose source를 기준으로 하는 신규 cutover의 gate를 통과하지 않음.

- [ ] **Step 1: cutover source runtime mismatch failing test 작성**

```python
def test_cutover_rejects_k3s_pvc_backup_evidence_for_compose_source(self):
    result = self.run_cutover_go(evidence_source_runtime="k3s-pvc")
    self.assertNotEqual(result.returncode, 0)
    self.assertIn("encrypted backup evidence is missing or invalid", result.stderr)
```

- [ ] **Step 2: 새 test가 현재 behavior에서 실패하는지 확인**

Run: `python3 -m unittest tests.test_k8s_portal_cutover.PortalCutoverContractTests -v`

Expected: source runtime을 구분하지 않는 현재 implementation 때문에 새 테스트가 실패함.

- [ ] **Step 3: cutover evidence assertion 최소 변경**

```bash
assert_compose_backup_evidence() {
  assert_backup_evidence || return 1
  awk -F= '$1 == "source_runtime" && $2 == "compose-local" { found=1 } END { exit !found }' "$BACKUP_EVIDENCE"
}
```

`--go`의 기존 `assert_backup_evidence` 호출을 `assert_compose_backup_evidence`로 교체함. rollout, Caddy, Compose bridge, deployment manifest의 동작은 변경하지 않음.

- [ ] **Step 4: 운영 문서 계약 갱신**

문서에 다음을 명시함.

```text
compose-local evidence: Compose writer를 멈춰 생성한 cutover 전용 증적
k3s-pvc evidence: K3s PVC writer를 멈춰 생성한 운영 backup 증적
두 evidence는 source_digest가 같아도 상호 재사용하지 않음
```

실행 예시는 `--check` 먼저, 사용자 유지보수 창에서 `--go` 한 번으로 제시함. 자동 실행·cron·GitHub Actions 배포 작업은 추가하지 않음.

- [ ] **Step 5: Task 3 관련 테스트 실행**

Run:

```bash
python3 -m unittest \
  tests.test_k8s_portal_cutover \
  tests.test_k8s_portal_backup_verify \
  tests.test_k8s_portal_pvc_backup_verify \
  tests.test_validate_backup_evidence -v
git diff --check
```

Expected: 모든 관련 테스트 통과, whitespace error 없음.

- [ ] **Step 6: Task 3 커밋**

```bash
git add infra/k8s/tools/portal-cutover.sh \
  tests/test_k8s_portal_cutover.py \
  docs/k3s-flux-transition-draft.md
git commit -m "docs: Portal 백업 원본 경계 명시"
```

## Task 4: 통합 검증·독립 검토·PR

**Files:**
- Verify only: Task 1~3 변경 파일 전체

**Interfaces:**
- Consumes: 세 Task의 committed changes와 local test results.
- Produces: PR에 포함할 검증 증적 및 독립 검토 결과.

- [ ] **Step 1: 변경 범위 harness 실행**

```bash
paths_file=$(mktemp)
git diff --name-status -z --find-renames origin/main HEAD > "$paths_file"
python3 scripts/run_change_harness.py \
  --input "$paths_file" \
  --input-format git-name-status-z \
  --check-result maintenance=success \
  --agent-context
rm -f "$paths_file"
```

Expected: `ready_for_review`. 서버 기동·스케줄러·CI workflow 경로가 포함되면 중단함.

- [ ] **Step 2: 관련 테스트·정적 검사 1회 실행**

```bash
bash -n infra/k8s/tools/portal-backup-verify.sh
bash -n infra/k8s/tools/portal-pvc-backup-verify.sh
python3 -m unittest \
  tests.test_validate_backup_evidence \
  tests.test_k8s_portal_backup_verify \
  tests.test_k8s_portal_pvc_backup_verify \
  tests.test_k8s_portal_cutover -v
git diff --check
```

Expected: 모든 명령 exit 0. 전체 unittest discovery는 기존 서비스별 import 충돌 때문에 이 작업의 성공 기준으로 사용하지 않음.

- [ ] **Step 3: 독립 검토 수행**

검토자는 아래를 확인함.

```text
1. 실패·signal·upload 오류에서 original replica 복구가 보장되는지
2. reader Pod가 두 PVC를 readOnly mount하는지
3. source_runtime 값이 compose-local/k3s-pvc 외에는 허용되지 않는지
4. 로그·Git·evidence에 비밀값, private key, host path가 없는지
5. Caddy, Tunnel, Compose, scheduler, server-startup 변경이 없는지
```

P1 이상 결함이 있으면 구현 담당이 최소 수정 후 관련 테스트를 한 번만 재실행하고, 검토자가 재확인함.

- [ ] **Step 4: PR 준비**

```bash
git status --short
git push -u origin codex/portal-pvc-backup
gh pr create --base main --head codex/portal-pvc-backup \
  --title 'feat: Portal PVC 암호화 백업 검증 추가' \
  --body-file /tmp/portal-pvc-backup-pr.md
```

PR 본문에는 코드가 자동으로 N100 `--go` 백업을 실행하지 않으며, 병합 후에도 운영자는 먼저 `--check`를 실행해야 한다는 사실을 포함함. CI와 독립 검토가 통과한 뒤 사용자 병합 승인을 요청함.

## Plan Self-Review

| 점검 | 결과 |
|---|---|
| Spec coverage | runtime 경계, 실제 PVC source, writer 중지, reader Pod, 암호화·Drive 복원, evidence, 원복, 테스트, 운영 절차를 Task 1~4에 매핑함 |
| Placeholder scan | 미정 표기·모호한 구현 지시 없음 |
| Interface consistency | `source_runtime`, `compose-local`, `k3s-pvc`, `portal_pvc_backup` 이름을 전 Task에서 동일하게 사용함 |
| Scope check | Portal 단일 서비스와 backup evidence 경계만 포함함. monitoring·Telegram·다른 서비스 전환은 제외함 |
