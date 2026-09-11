# Portal PVC Backup CronJob Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** WSL user systemd credential 제약을 제거하고, 사전 시딩된 백업 Secret을 참조하는 K3s CronJob으로 Portal PVC의 일일 암호화 백업과 복원 검증을 실행함.

**Architecture:** backup runner는 in-cluster ServiceAccount와 read-only PVC mount를 사용해 기존 verifier를 `--go`로 실행함. verifier는 host 수동 실행과 in-cluster 실행을 명시적으로 분리하고, CronJob 경로에서는 최소 RBAC로 evidence와 Telegram backup status ConfigMap만 patch함.

**Tech Stack:** Bash, Kubernetes CronJob/RBAC/ConfigMap/Secret volume, K3s, rclone, age, SQLite, Python unittest.

**Spec:** `docs/superpowers/specs/2026-09-11-portal-pvc-backup-cronjob-design.md`

## Global Constraints

- Secret 값, rclone 설정, age identity, Telegram 자격 증명은 생성·출력·복제·Git 저장 금지.
- CronJob은 `portal-pvc-backup-verify.sh --go` 하나만 실행함.
- Portal PVC는 read-only mount만 사용하며 Portal 운영 Secret·PVC·Caddy·Tunnel·Compose writer를 변경하지 않음.
- CronJob은 `concurrencyPolicy: Forbid`, `restartPolicy: Never`, `backoffLimit: 0`을 사용함.
- CronJob ServiceAccount에는 Secret RBAC, `pods/exec`, Pod create/delete, 일반 Deployment patch 권한을 부여하지 않음.
- 기존 systemd timer는 CronJob 성공 검증 전까지 제거하지 않으며, 실제 활성화는 사용자 승인 후 수행함.
- 실제 적용 전후 외부 health 4개 endpoint를 10초 간격 3회씩 검증함.

---

### Task 1: verifier의 in-cluster 실행 경계

**Files:**
- Modify: `infra/k8s/tools/portal-pvc-backup-verify.sh`
- Modify: `tests/test_k8s_portal_pvc_backup_verify.py`

**Interfaces:**
- Consumes: `PORTAL_BACKUP_EXECUTION_MODE=host|in-cluster`
- Produces: host 실행은 기존 `sudo -n k3s kubectl` 계약을 유지하고, in-cluster 실행은 `kubectl`·고정 Secret file path·read-only mount 경로만 사용함.

- [ ] **Step 1: in-cluster 실행의 실패 테스트 작성**

```python
def test_in_cluster_mode_uses_kubectl_without_sudo_or_k3s(self):
    result = self.run_tool("--go", env={"PORTAL_BACKUP_EXECUTION_MODE": "in-cluster"})
    self.assertEqual(result.returncode, 0)
    self.assertNotIn("sudo -n k3s", self.calls())
    self.assertIn("kubectl -n personal-server", self.calls())
```

- [ ] **Step 2: 테스트가 현재 host 전용 구현에서 실패함을 확인**

Run: `python3 -m unittest tests.test_k8s_portal_pvc_backup_verify.PortalPvcBackupVerifyTests.test_in_cluster_mode_uses_kubectl_without_sudo_or_k3s`

Expected: FAIL because host `sudo -n k3s kubectl` path is used.

- [ ] **Step 3: 최소 실행 모드 abstraction 구현**

```bash
case "${PORTAL_BACKUP_EXECUTION_MODE:-host}" in
  host) KCTL=(sudo -n k3s kubectl) ;;
  in-cluster) KCTL=(kubectl) ;;
  *) exit 2 ;;
esac
kctl() { run_timeout "${PORTAL_KUBECTL_TIMEOUT_SECONDS:-120}" "${KCTL[@]}" "$@"; }
```

host 전용 sudo 사전 확인은 host mode에만 적용하고, 모든 Kubernetes 호출을 `kctl`로 통일함. in-cluster mode는 `/work` evidence와 fixed Secret files만 허용하고, Portal Pod exec·임시 reader Pod 생성 경로를 사용하지 않음.

- [ ] **Step 4: RED 테스트와 기존 verifier 테스트를 통과시킴**

Run: `python3 -m unittest tests.test_k8s_portal_pvc_backup_verify`

Expected: PASS.

### Task 2: in-cluster evidence·Telegram 상태 기록 완성

**Files:**
- Modify: `infra/k8s/tools/portal-pvc-backup-verify.sh`
- Modify: `tests/test_k8s_portal_pvc_backup_verify.py`

**Interfaces:**
- Consumes: in-cluster ServiceAccount의 fixed ConfigMap `portal-pvc-backup-evidence`, `sre-telegram-backup-status` get/patch 권한.
- Produces: host mode와 분리된 in-cluster evidence persistence 및 고정 4-field Telegram backup status patch.

- [ ] **Step 1: evidence/status 기록 RED 테스트 작성**

```python
def test_in_cluster_failure_patches_only_allowlisted_telegram_status_after_portal_restore(self):
    result, calls, _ = self.run_tool("--go", execution_mode="in-cluster", fail_at="upload")
    self.assertNotEqual(result.returncode, 0)
    self.assertLess(calls.index("scale deployment/portal-web --replicas=1"), calls.index("patch configmap sre-telegram-backup-status"))
    self.assertNotIn("apply -f", calls)
```

- [ ] **Step 2: 테스트가 현재 verifier의 ConfigMap 미기록으로 실패함을 확인**

Run: `python3 -m unittest tests.test_k8s_portal_pvc_backup_verify.PortalPvcBackupVerifyTests.test_in_cluster_failure_patches_only_allowlisted_telegram_status_after_portal_restore`

Expected: FAIL because the verifier does not patch either ConfigMap.

- [ ] **Step 3: 고정 ConfigMap get/patch 구현**

in-cluster mode는 runtime marker를 검증하지 않음. evidence는 non-secret data field만 read/patch하며, Telegram 상태는 `run_id`, `status`, `completed_at`, `stage` 4개 allow-listed field만 JSON patch로 기록함. failure status 기록은 Portal cleanup 이후에만 수행함.

- [ ] **Step 4: host와 in-cluster verifier 테스트를 통과시킴**

Run: `python3 -m unittest tests.test_k8s_portal_pvc_backup_verify`

Expected: PASS.

### Task 3: backup runner image와 CronJob 최소 권한 매니페스트

**Files:**
- Create: `infra/k8s/backup-automation/Dockerfile`
- Create: `infra/k8s/backup-automation/portal-pvc-backup-cronjob.yaml`
- Create: `tests/test_k8s_portal_pvc_backup_cronjob.py`

**Interfaces:**
- Consumes: 사전 시딩 Secret `portal-pvc-backup-runtime`의 `rclone-config`, `rclone-config-passphrase`, `age-recipient`, `age-identity` file keys.
- Produces: `ServiceAccount/portal-pvc-backup`, 한정 Role/RoleBinding, evidence/status ConfigMap, suspended-by-default CronJob.

- [ ] **Step 1: 매니페스트 정적 계약 RED 테스트 작성**

```python
def test_cronjob_is_singleton_and_never_exposes_backup_secret_as_environment(self):
    cronjob = load_cronjob()
    self.assertEqual(cronjob["spec"]["concurrencyPolicy"], "Forbid")
    self.assertEqual(cronjob["spec"]["jobTemplate"]["spec"]["backoffLimit"], 0)
    self.assertEqual(command(cronjob), ["/opt/personal-server/portal-pvc-backup-verify.sh", "--go"])
    self.assertFalse(has_secret_env(cronjob))
```

- [ ] **Step 2: 테스트가 manifest 부재로 실패함을 확인**

Run: `python3 -m unittest tests.test_k8s_portal_pvc_backup_cronjob`

Expected: FAIL because the CronJob manifest does not exist.

- [ ] **Step 3: 고정 runner와 최소 RBAC 매니페스트 구현**

```yaml
spec:
  suspend: true
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      backoffLimit: 0
      template:
        spec:
          serviceAccountName: portal-pvc-backup
          restartPolicy: Never
```

Secret은 `items`가 정해진 read-only volume만 사용함. Role은 `deployments/scale`, 고정 PVC, 고정 evidence/status ConfigMap 이외의 쓰기 권한을 갖지 않음. Pod exec, Secret RBAC, dynamic Pod resource 권한은 선언하지 않음.

- [ ] **Step 4: 정적 테스트와 client dry-run을 통과시킴**

Run: `python3 -m unittest tests.test_k8s_portal_pvc_backup_cronjob && docker build --check -f infra/k8s/backup-automation/Dockerfile .`

Expected: PASS. 실제 image build/import는 배포 승인 단계에서만 수행함.

### Task 4: 안전한 설치·사전 점검·운영 문서

**Files:**
- Create: `infra/k8s/tools/portal-pvc-backup-cronjob.sh`
- Modify: `infra/k8s/README.md`
- Modify: `docs/recovery-drill.md`
- Create: `tests/test_k8s_portal_pvc_backup_cronjob_install.py`

**Interfaces:**
- Consumes: `--preflight`, `--render`, `--apply`, `--activate`, `--status`.
- Produces: Secret key-name-only 확인, manifest dry-run, RWO mount capability probe, activation 전 systemd timer inactive 확인.

- [ ] **Step 1: 사전 점검 실패 RED 테스트 작성**

```python
def test_activate_refuses_when_legacy_timer_is_active_or_secret_contract_is_missing(self):
    result = self.run_tool("--activate", legacy_timer="active", secret_keys=[])
    self.assertNotEqual(result.returncode, 0)
    self.assertNotIn("patch cronjob", self.calls())
```

- [ ] **Step 2: 테스트가 installer 부재로 실패함을 확인**

Run: `python3 -m unittest tests.test_k8s_portal_pvc_backup_cronjob_install`

Expected: FAIL because the installer does not exist.

- [ ] **Step 3: activation guard와 문서 구현**

```bash
case "$mode" in
  --preflight) verify_secret_key_names; verify_rwo_mount_capability ;;
  --render) kubectl apply --dry-run=client -f "$MANIFEST" ;;
  --apply) verify_secret_key_names; apply_suspended_cronjob ;;
  --activate) verify_legacy_timer_inactive; verify_rwo_mount_capability; unsuspend_cronjob ;;
esac
```

Secret 내용 대신 이름·key 목록만 검사함. `--activate`는 기존 user timer가 inactive인 상태에서만 suspended CronJob을 해제함. 문서는 SOPS/age 또는 승인된 Secret Manager 사전 시딩만 안내하고 Secret 입력 명령·값을 포함하지 않음.

- [ ] **Step 4: installer·문서·관련 테스트를 통과시킴**

Run: `python3 -m unittest tests.test_k8s_portal_pvc_backup_cronjob_install tests.test_k8s_portal_pvc_backup_cronjob tests.test_k8s_portal_pvc_backup_verify tests.test_documentation_index`

Expected: PASS.

### Task 5: 독립 보안·운영 검토와 배포 준비

**Files:**
- Review: 모든 Task 1~3 변경

- [ ] **Step 1: 권한·Secret 경계·legacy scheduler 단일화 독립 검토**

검토자는 Secret 값/환경 변수 노출, `pods/exec`, Secret RBAC, broad patch/delete 권한, 활성 timer와 unsuspended CronJob 공존을 차단 기준으로 확인함.

- [ ] **Step 2: 위험 기반 검증 수행**

Run: `git diff --check`, 관련 unittest 묶음, manifest client dry-run, `python3 scripts/verify_change_scope.py` 및 change harness 결과 갱신.

Expected: 모든 검증 PASS. 실제 Secret 사전 시딩·image build/import·CronJob activation·외부 health 검증은 사용자 배포 승인 후 수행함.
