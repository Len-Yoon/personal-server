# Telegram SRE 최종 안전 보완 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telegram SRE 알림의 전달 범위, relay 실행 사용자, Helm rollback의 Secret 경계를 배포 전에 fail-closed로 완성함.

**Architecture:** Alertmanager root route는 수신 동작이 없는 `sre-telegram-noop`으로 고정하고 `sre_telegram="true"` child만 relay로 보냄. Relay image와 Pod security context는 numeric UID/GID `10001`로 일치시킴. Helm rollback은 이전 deployed revision 번호만 보관·복구하고 values·manifest 전체를 기록하지 않음.

**Tech Stack:** K3s manifests, Alertmanager YAML, Python 3.11 + PyYAML, Bash, unittest, Docker.

**Spec:** `docs/superpowers/specs/2026-09-02-k3s-telegram-sre-alerting-design.md`

## Global Constraints

- `sre_telegram="true"` route만 Telegram relay로 전달함.
- bearer 값과 Kubernetes Secret `.data`를 Git·문서·명령 출력·임시 snapshot에 기록하지 않음.
- Compose, Portal, Caddy, 외부 Ingress·NodePort·LoadBalancer, 서버 기동 스크립트, Windows bootstrap, 기존 scheduler를 수정하지 않음.
- 실제 N100 Secret seed, Helm/K3s apply, Telegram delivery는 별도 명시 승인 전 수행하지 않음.

---

### Task 1: Alertmanager 고정 전달 범위 완성

**Files:**
- Modify: `docs/superpowers/specs/2026-09-02-k3s-telegram-sre-alerting-design.md`
- Modify: `infra/k8s/sre-telegram/alertmanager.yaml.tmpl`
- Modify: `infra/k8s/sre-telegram/alertmanager-config.contract.yaml`
- Modify: `infra/k8s/tools/validate-sre-alertmanager-config.py`
- Modify: `tests/test_validate_sre_alertmanager_config.py`

**Interfaces:** Exact schema has root receiver `sre-telegram-noop`, fixed root grouping and `4h` repeat, exactly one `sre_telegram="true"` child to `sre-telegram-relay`, and exactly two receivers. Validator accepts only this single YAML document and rejects `global`, unknown/duplicate keys, extra routes/receivers, and root relay.

- [ ] **Step 1: Write failing tests**

```python
def test_accepts_only_noop_root_and_sre_labeled_relay_child(self):
    document = self.template_document()
    self.assertEqual(document["route"]["receiver"], "sre-telegram-noop")
    self.assertEqual(self.run_validator(TEMPLATE.read_text()).returncode, 0)

def test_rejects_root_relay_and_nonempty_global(self):
    root_relay = self.template_document()
    root_relay["route"]["receiver"] = "sre-telegram-relay"
    with_global = self.template_document()
    with_global["global"] = {"resolve_timeout": "5m"}
    self.assert_rejected(root_relay)
    self.assert_rejected(with_global)
```

- [ ] **Step 2: Run RED**

Run: `python3 -m unittest tests/test_validate_sre_alertmanager_config.py -v`

Expected: FAIL because root relay and an optional `global` mapping are currently accepted.

- [ ] **Step 3: Implement the exact schema**

Use the two receiver entries below in both template and non-secret contract. Remove `global` from validator root keys and update spec wording: non-matching alerts reach the no-op receiver, never Telegram.

```yaml
receivers:
  - name: sre-telegram-noop
  - name: sre-telegram-relay
    webhook_configs:
      - url: http://sre-telegram-relay.monitoring.svc:8080/alertmanager
        send_resolved: true
        http_config:
          authorization:
            type: Bearer
            credentials_file: /etc/alertmanager/secrets/sre-telegram-relay-runtime/alertmanager_auth_token
```

- [ ] **Step 4: Run GREEN and commit**

Run: `python3 -m unittest tests/test_validate_sre_alertmanager_config.py -v && python3 -m py_compile infra/k8s/tools/validate-sre-alertmanager-config.py && git diff --check`

Expected: PASS.

Commit: `fix: Telegram Alertmanager 전달 범위 제한`

### Task 2: Relay numeric non-root 실행 계약

**Files:**
- Modify: `sre-telegram-relay/Dockerfile`
- Modify: `infra/k8s/sre-telegram/base.yaml`
- Modify: `tests/test_k8s_sre_telegram_manifests.py`

**Interfaces:** Image and Pod use UID/GID `10001`. `/app` is owned by `10001:10001`; Pod-level `runAsUser`, `runAsGroup`, `fsGroup` are `10001` and retain existing `runAsNonRoot` and hardening.

- [ ] **Step 1: Write failing test**

```python
def test_relay_uses_numeric_non_root_user_contract(self):
    self.assertIn("addgroup --gid 10001", dockerfile)
    self.assertIn("adduser --uid 10001", dockerfile)
    self.assertIn("USER 10001:10001", dockerfile)
    self.assertEqual(pod_security["runAsUser"], 10001)
    self.assertEqual(pod_security["runAsGroup"], 10001)
    self.assertEqual(pod_security["fsGroup"], 10001)
```

- [ ] **Step 2: Run RED, implement, run GREEN**

Run RED: `python3 -m unittest tests/test_k8s_sre_telegram_manifests.py -v`

Implement exactly: `addgroup --system --gid 10001 relay`; `adduser --system --uid 10001 --ingroup relay relay`; `COPY --chown=10001:10001`; `USER 10001:10001`; add the three numeric Pod security fields.

Run GREEN: `python3 -m unittest tests/test_k8s_sre_telegram_manifests.py tests/test_sre_telegram_relay.py -v`

Run build check: `docker build -t personal-server-sre-telegram-relay:contract-test sre-telegram-relay && docker image inspect personal-server-sre-telegram-relay:contract-test --format '{{.Config.User}}'`

Expected user: `10001:10001`.

Commit: `fix: Telegram relay numeric non-root 실행 적용`

### Task 3: Helm rollback Secret snapshot 제거

**Files:**
- Modify: `infra/k8s/tools/sre-telegram-install.sh`
- Modify: `tests/test_k8s_sre_telegram_tools.py`
- Modify: `infra/k8s/README.md`

**Interfaces:** `capture_previous_helm_state` retains only a previous deployed numeric revision in memory. `verify_or_restore_helm_release` may `helm rollback` then requires deployed `helm status --output json`. It never invokes `helm get values`, `helm get manifest`, or creates a Helm snapshot directory.

- [ ] **Step 1: Write failing rollback tests**

```python
def test_install_never_snapshots_helm_values_or_manifest(self):
    result, calls = self.run_tool(...)
    self.assertNotIn("helm get values", calls)
    self.assertNotIn("helm get manifest", calls)

def test_failed_upgrade_rolls_back_prior_revision_and_requires_deployed_status(self):
    result, calls = self.run_tool(...)
    self.assertIn("helm rollback personal-server-monitoring 2 --namespace monitoring", calls)
    self.assertIn("helm status personal-server-monitoring --namespace monitoring --output json", calls)
```

- [ ] **Step 2: Run RED**

Run: `python3 -m unittest tests/test_k8s_sre_telegram_tools.py -v`

Expected: FAIL because Helm values and manifest are currently captured.

- [ ] **Step 3: Remove data-bearing snapshots**

Delete `HELM_SNAPSHOT_DIR`, all snapshot cleanup, `helm get values`, `helm get manifest`, and content comparisons. Preserve revision extraction, atomic upgrade, failure trap, created-resource rollback and fixed output. After rollback, only deployed status is required; rollback creating a new revision is valid. Update README to say Helm values/manifests are deliberately not archived.

- [ ] **Step 4: Run GREEN and commit**

Run: `python3 -m unittest tests/test_k8s_sre_telegram_tools.py -v && bash -n infra/k8s/tools/sre-telegram-install.sh && git diff --check`

Expected: PASS.

Commit: `fix: Telegram Helm rollback 비밀값 snapshot 제거`

### Task 4: 최종 통합 검증과 live gate

**Files:**
- Test: `tests/test_validate_sre_alertmanager_config.py`, `tests/test_k8s_sre_telegram_manifests.py`, `tests/test_k8s_sre_telegram_tools.py`, `tests/test_sre_telegram_relay.py`, monitoring tests.

- [ ] **Step 1: Run affected suite**

Run: `python3 -m unittest tests/test_validate_sre_alertmanager_config.py tests/test_k8s_sre_telegram_manifests.py tests/test_k8s_sre_telegram_tools.py tests/test_sre_telegram_relay.py tests/test_k8s_monitoring_tools.py tests/test_k8s_monitoring_values.py -v`

Expected: PASS.

- [ ] **Step 2: Static and scope checks**

Run: `git diff --check main...HEAD`

Expected: PASS.

Generate a NUL path record and run `python3 scripts/run_change_harness.py --input <record> --input-format git-name-status-z --check-result maintenance=success --agent-context`.

Expected: `ready_for_review`.

- [ ] **Step 3: Independent review and stop**

Verify no non-matching alert reaches relay, duplicate/unknown/non-empty global YAML is rejected, image identity is numeric non-root, tools/tests do not capture Helm values/manifests or Secret `.data`, and prohibited paths are unchanged. Do not run N100 Secret seed, Helm upgrade, K3s apply, or Telegram delivery; require explicit user approval after review.

## Self-Review

- Tasks 1–3 map exactly to the final review P1/P2 findings; Task 4 verifies all cross-cutting boundaries.
- No placeholders remain; every task names files, target behavior, tests and commands.
- Task 1 exact Alertmanager contract is consumed by install preflight; Tasks 2–3 are independent hardening boundaries.
