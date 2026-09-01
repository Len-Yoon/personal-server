# K3s Pod 자동복구 실습 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 서비스에 영향 없이 K3s liveness 실패와 자동 컨테이너 재시작을 검증하는 operator-only 실습 도구를 추가함.

**Architecture:** Bash 도구가 고유 namespace의 BusyBox Deployment를 생성하고 sentinel 파일로 liveness failure를 유도함. restart count 증가와 Ready 복귀를 확인한 뒤 정확한 namespace만 정리함. Python unittest는 fake `sudo k3s kubectl`을 사용해 명령 경계·실패 정리를 검증함.

**Tech Stack:** Bash, K3s kubectl, BusyBox, Python unittest.

**Spec:** `docs/superpowers/specs/2026-09-01-k3s-auto-recovery-lab-design.md`

## Global Constraints

- `sre-recovery-lab-<run-id>` 외의 Kubernetes 리소스를 만들거나 삭제하지 않음.
- 실행은 명시적 `--run`에서만 허용하며 run id는 소문자 영문·숫자·하이픈만 허용함.
- Portal, Compose, Caddy, Flux, PVC, SQLite, Secret, 서버 기동, scheduler를 변경하지 않음.
- `EXIT` trap을 사용하지 않으며 실패·signal 정리는 정확한 namespace를 대상으로 수행함.
- 자동복구 성공은 restart count 증가와 Pod Ready 복귀로 판정하며 event는 보조 증적으로만 기록함.

---

### Task 1: 고립된 Pod 자동복구 실습 도구

**Files:**
- Create: `infra/k8s/tools/sre-pod-recovery-lab.sh`
- Create: `tests/test_k8s_sre_pod_recovery_lab.py`

**Interfaces:**
- Produces: `sre_pod_recovery=PASS|FAIL`, `sre_pod_recovery_run_id=<id>`.
- Commands: `--run`, `--cleanup <run-id>`.

- [ ] **Step 1: failing contract test 작성**

```python
def test_lab_uses_isolated_deployment_and_liveness_sentinel(self):
    text = SCRIPT.read_text(encoding="utf-8")
    for required in (
        'NS="sre-recovery-lab-${run_id_lc}"',
        'kind: Namespace',
        'kind: Deployment',
        'image: busybox:1.36',
        'livenessProbe:',
        'test ! -f /tmp/force-liveness-failure',
        'touch /tmp/force-liveness-failure',
        'restartCount',
        '--for=condition=Ready',
    ):
        self.assertIn(required, text)
    self.assertNotIn('portal-web', text)
    self.assertNotIn('docker compose', text.lower())
```

- [ ] **Step 2: RED 확인**

Run: `python3 -m unittest tests.test_k8s_sre_pod_recovery_lab.K3sSrePodRecoveryLabTest.test_lab_uses_isolated_deployment_and_liveness_sentinel -v`

Expected: FAIL because the script does not exist.

- [ ] **Step 3: 최소 실행 도구 작성**

```bash
NS="sre-recovery-lab-${run_id_lc}"
POD_LABEL='app.kubernetes.io/name=sre-pod-recovery'

# Deployment container command
command: ["sh", "-c", "while true; do sleep 3600; done"]
# Both probes
exec:
  command: ["sh", "-c", "test ! -f /tmp/force-liveness-failure"]
```

`--run`은 namespace가 없는지 확인하고 manifest를 생성한 뒤 Pod Ready를 기다림. baseline restart count를 읽고 sentinel을 만들며, 제한 시간 안에 restart count가 증가하고 Ready로 돌아오는지 확인함. 성공·실패에 cleanup을 명시적으로 호출함. `--cleanup`은 유효한 run id와 고정 접두사 namespace만 삭제하고 부재를 확인함.

- [ ] **Step 4: 실패 cleanup 행동 테스트 작성 및 GREEN 확인**

```python
def test_apply_failure_deletes_only_the_current_lab_namespace(self):
    # fake sudo records k3s kubectl calls; apply exits 42 and delete exits 0.
    result = subprocess.run(["bash", str(SCRIPT), "--run"], env={...}, check=False)
    self.assertNotEqual(result.returncode, 0)
    self.assertIn("delete namespace sre-recovery-lab-apply-failure", calls.read_text())
    self.assertNotIn("portal-web", calls.read_text())
```

Run: `python3 -m unittest tests.test_k8s_sre_pod_recovery_lab -q`

- [ ] **Step 5: commit**

```bash
git add infra/k8s/tools/sre-pod-recovery-lab.sh tests/test_k8s_sre_pod_recovery_lab.py
git commit -m "feat: K3s Pod 자동복구 실습 추가"
```

### Task 2: 운영 안내와 회귀 검증

**Files:**
- Modify: `infra/k8s/README.md`
- Modify: `tests/test_k8s_sre_pod_recovery_lab.py`

**Interfaces:**
- Consumes: Task 1의 `--run`, `--cleanup <run-id>` contract.
- Produces: 적용 없는 N100 operator runbook와 문서 계약 테스트.

- [ ] **Step 1: 문서 계약 failing test 작성**

```python
def test_readme_documents_operator_only_recovery_lab(self):
    text = README.read_text(encoding="utf-8")
    self.assertIn("sre-pod-recovery-lab.sh --run", text)
    self.assertIn("sre-pod-recovery-lab.sh --cleanup", text)
    self.assertIn("Portal", text)
    self.assertIn("변경하지 않는다", text)
```

- [ ] **Step 2: RED 확인**

Run: `python3 -m unittest tests.test_k8s_sre_pod_recovery_lab.K3sSrePodRecoveryLabTest.test_readme_documents_operator_only_recovery_lab -v`

Expected: FAIL because the guide is absent.

- [ ] **Step 3: README 운영 안내 추가**

`infra/k8s/README.md`에 lab가 GitOps resource나 production deploy가 아님을 명시함. `bash infra/k8s/tools/sre-pod-recovery-lab.sh --run`과 비정상 중단 때만 사용할 `--cleanup <run-id>`를 기록함. expected PASS 출력, namespace 격리, Pod restart/Ready 기준, Portal·Compose·Caddy·scheduler를 바꾸지 않는 경계를 포함함.

- [ ] **Step 4: GREEN 및 전체 검증**

Run: `python3 -m unittest tests.test_k8s_sre_pod_recovery_lab tests.test_k8s_portal_nodeport_connectivity_smoke tests.test_verify_change_scope -q`

Run: `bash -n infra/k8s/tools/sre-pod-recovery-lab.sh && git diff --check`

Run: `printf '%s\n' infra/k8s/tools/sre-pod-recovery-lab.sh tests/test_k8s_sre_pod_recovery_lab.py infra/k8s/README.md > /tmp/sre-recovery-lab-paths.txt && python3 scripts/run_change_harness.py --input /tmp/sre-recovery-lab-paths.txt --check-result maintenance=success --agent-context`

- [ ] **Step 5: commit**

```bash
git add infra/k8s/README.md tests/test_k8s_sre_pod_recovery_lab.py
git commit -m "docs: 자동복구 실습 운영 안내 추가"
```

## Plan self-review

| 설계 요구사항 | 구현 Task |
| --- | --- |
| isolated namespace와 stateless Deployment | Task 1 |
| liveness failure와 restart/Ready 복구 확인 | Task 1 |
| exact cleanup 및 signal/failure 경계 | Task 1 |
| operator-only guide와 production exclusion | Task 2 |
| unit·syntax·harness 검증 | Task 1, Task 2 |
