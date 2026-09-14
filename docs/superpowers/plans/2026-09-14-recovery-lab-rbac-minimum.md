# Recovery Lab RBAC Minimum Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 분기 SRE 감사의 `pods/exec` 권한을 제거하고, 제한된 ConfigMap 신호 기반으로 같은 Pod의 liveness 복구를 검증한다.

**Architecture:** recovery lab Deployment는 읽기 전용 ConfigMap을 관찰해 컨테이너당 한 번만 health 파일을 제거한다. Runner는 특정 ConfigMap 신호를 초기화·활성화·정리하고, 동일 Pod UID의 재시작 및 Ready 복귀를 판정한다.

**Tech Stack:** Kubernetes YAML, Bash, Python unittest, kubectl

**Spec:** `docs/superpowers/specs/2026-09-14-recovery-lab-rbac-minimum-design.md`

## Global Constraints

- `sre-recovery-lab`과 `quarterly-sre-audit`만 수정함.
- `pods/exec` 권한을 추가하거나 유지하지 않음.
- trigger ConfigMap에는 `get`, `patch`만 부여하며 resourceNames로 제한함.
- Secret, PVC, Portal, Caddy, 운영 데이터는 수정하지 않음.
- 정상·실패 종료 모두 trigger를 `false`로 복구하고 Deployment replicas 0을 확인함.

---

### Task 1: ConfigMap 신호와 최소 RBAC 계약

**Files:**
- Modify: `infra/k8s/sre-audit-automation/quarterly-sre-audit-cronjob.yaml`
- Test: `tests/test_k8s_quarterly_sre_audit_cronjob.py`

**Interfaces:**
- Produces: `ConfigMap/sre-pod-recovery-trigger` in `sre-recovery-lab` with `data.trigger: "false"`.
- Produces: Runner ServiceAccount Role access limited to `configmaps/sre-pod-recovery-trigger`, verbs `get, patch`.

- [ ] **Step 1: Write failing manifest-contract tests**

```python
self.assertEqual(find("ConfigMap", "sre-pod-recovery-trigger", "sre-recovery-lab")["data"], {"trigger": "false"})
self.assertIn({"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["sre-pod-recovery-trigger"], "verbs": ["get", "patch"]}, lab_rules)
self.assertNotIn("pods/exec", str(lab_rules))
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `python3 -m unittest tests.test_k8s_quarterly_sre_audit_cronjob.QuarterlySreAuditCronJobTests.test_rbac_is_limited_to_audit_reads_fixed_status_and_recovery_lab -v`

- [ ] **Step 3: Add the ConfigMap and replace the exec RBAC rule**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: sre-pod-recovery-trigger
  namespace: sre-recovery-lab
data:
  trigger: "false"
```

```yaml
- apiGroups: [""]
  resources: [configmaps]
  resourceNames: [sre-pod-recovery-trigger]
  verbs: [get, patch]
```

- [ ] **Step 4: Run the focused manifest-contract tests and verify they pass**

Run: `python3 -m unittest tests.test_k8s_quarterly_sre_audit_cronjob -v`

### Task 2: Exec 없는 단발성 장애 주입과 Runner 정리

**Files:**
- Modify: `infra/k8s/sre-audit-automation/quarterly-sre-audit-cronjob.yaml`
- Modify: `infra/k8s/tools/quarterly-sre-audit-runner.sh`
- Test: `tests/test_k8s_quarterly_sre_audit_cronjob.py`

**Interfaces:**
- Consumes: `sre-pod-recovery-trigger` ConfigMap with `data.trigger` string.
- Produces: exec 없이 recovery restart와 Ready 복귀를 확인하는 `check_recovery_lab`.

- [ ] **Step 1: Write a failing runner test**

```python
self.assertIn("patch configmap sre-pod-recovery-trigger", calls)
self.assertNotIn("exec recovery-pod", calls)
self.assertIn('"trigger":"false"', calls)
```

- [ ] **Step 2: Run the test and verify it fails because the runner still execs the Pod**

Run: `python3 -m unittest tests.test_k8s_quarterly_sre_audit_cronjob.QuarterlySreAuditCronJobTests.test_runner_emits_relay_compatible_payload_after_a_successful_run -v`

- [ ] **Step 3: Mount and consume the trigger ConfigMap**

Use a read-only `/var/run/recovery-trigger` mount. The shell command must create `/tmp/healthy`, record `/tmp/recovery-fault-injected` before removing health only when trigger equals `true`, and keep polling at a short interval.

- [ ] **Step 4: Replace `kubectl exec` with bounded ConfigMap patches**

Add a shell helper which applies `{"data":{"trigger":"true"|"false"}}` only to the fixed ConfigMap. Reset to false before scale-up, activate after baseline, and reset in `finalize` before scale-down. A failed reset makes the recovery result fail.

- [ ] **Step 5: Update the fake kubectl state machine and all focused tests**

The fake command must make a restart visible only after the `trigger=true` patch, support injected trigger-patch failures, and persist calls for assertions.

- [ ] **Step 6: Run focused runner tests and verify they pass**

Run: `python3 -m unittest tests.test_k8s_quarterly_sre_audit_cronjob -v`

### Task 3: Full contract and security verification

**Files:**
- Verify only: `infra/k8s/sre-audit-automation/quarterly-sre-audit-cronjob.yaml`
- Verify only: `infra/k8s/tools/quarterly-sre-audit-runner.sh`
- Verify only: `tests/test_k8s_quarterly_sre_audit_cronjob.py`

- [ ] **Step 1: Run the whole quarterly SRE audit test module**

Run: `python3 -m unittest tests.test_k8s_quarterly_sre_audit_cronjob -v`

- [ ] **Step 2: Validate YAML documents**

Run: `python3 -c 'import yaml,pathlib; list(yaml.safe_load_all(pathlib.Path("infra/k8s/sre-audit-automation/quarterly-sre-audit-cronjob.yaml").read_text())); print("yaml=PASS")'`

- [ ] **Step 3: Verify the intended security boundary directly**

Run: `! rg -n 'pods/exec|kubectl .* exec' infra/k8s/sre-audit-automation/quarterly-sre-audit-cronjob.yaml infra/k8s/tools/quarterly-sre-audit-runner.sh && echo rbac_exec=PASS`

- [ ] **Step 4: Run the repository change harness with the actual changed-path list and record each check result**

Run: `python3 scripts/run_change_harness.py --input <git-name-status-z-file> --input-format git-name-status-z --agent-context`
