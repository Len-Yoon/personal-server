# Alertmanager 고정 템플릿 전환 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telegram SRE 경고용 Alertmanager 설정을 N100 전용 고정 템플릿으로 생성·검증하여 임의 route tree 해석을 제거함.

**Architecture:** Git에는 비밀값 없는 템플릿과 구조 검증기만 둠. N100 운영자가 Telegram relay 인증값을 삽입한 0600 임시 파일을 만들고, 검증기가 템플릿의 허용된 단일 route/receiver 형태와 `amtool check-config`를 통과할 때만 Secret seed·설치를 허용함.

**Tech Stack:** Alertmanager YAML, Python 3 표준 라이브러리, PyYAML, Bash, unittest, Helm/K3s.

**Spec:** `docs/superpowers/specs/2026-09-02-k3s-telegram-sre-alerting-design.md`

## Global Constraints

- Compose, Portal, Caddy, 외부 Ingress·NodePort·LoadBalancer, 서버 기동 스크립트, Windows bootstrap, 기존 스케줄러를 수정하지 않음.
- Git·문서·테스트 출력·로그에 Telegram token, chat ID, Alertmanager bearer 값 또는 Kubernetes Secret `.data`를 기록하지 않음.
- Alertmanager Kubernetes Secret은 N100에서만 수동 seed하며, `--apply` 전까지 클러스터 상태를 변경하지 않음.
- 템플릿 외 route, receiver, `continue: true`, 중첩 route는 1차 범위에서 모두 fail-closed 처리함.

---

## File Structure

| 파일 | 책임 |
|---|---|
| `infra/k8s/sre-telegram/alertmanager.yaml.tmpl` | SRE 전용 비밀값 없는 Alertmanager 설정 골격 |
| `infra/k8s/tools/validate-sre-alertmanager-config.py` | 임시 N100 config의 정확한 구조 검증, 출력 없음 |
| `infra/k8s/tools/sre-telegram-preflight.sh` | `amtool` + 구조 검증기 실행, 실패 시 중단 |
| `infra/k8s/tools/sre-telegram-secret-template.sh` | 템플릿 복사·0600·검증·seed 순서 안내 |
| `tests/test_validate_sre_alertmanager_config.py` | 허용 템플릿과 모든 route 우회 거부 테스트 |
| `tests/test_k8s_sre_telegram_tools.py` | preflight fail-closed·비밀 비출력 계약 |
| `infra/k8s/README.md` | N100 운영 절차와 템플릿 소유권 문서 |

## Task 1: 고정 Alertmanager 템플릿과 구조 검증기

**Files:**
- Create: `infra/k8s/sre-telegram/alertmanager.yaml.tmpl`
- Create: `infra/k8s/tools/validate-sre-alertmanager-config.py`
- Create: `tests/test_validate_sre_alertmanager_config.py`

**Interfaces:**
- Consumes: `validate-sre-alertmanager-config.py CONFIG_PATH`.
- Produces: exit 0 only for one root route, one child `sre_telegram="true"` route, one relay receiver and one webhook configuration.

- [ ] **Step 1: Write failing validator tests**

```python
def test_accepts_exact_sre_template_with_operator_supplied_bearer_file(): ...
def test_rejects_any_extra_route_or_receiver(): ...
def test_rejects_continue_or_nested_routes(): ...
def test_rejects_wrong_matcher_receiver_url_or_credentials_file(): ...
```

- [ ] **Step 2: Run RED**

Run: `python3 -m unittest tests/test_validate_sre_alertmanager_config.py -v`  
Expected: FAIL because the template and validator do not exist.

- [ ] **Step 3: Create the non-secret template**

```yaml
route:
  receiver: sre-telegram-relay
  group_by: [alertname, namespace, pod, deployment, persistentvolumeclaim]
  repeat_interval: 4h
  routes:
    - matchers: ['sre_telegram="true"']
      receiver: sre-telegram-relay
receivers:
  - name: sre-telegram-relay
    webhook_configs:
      - url: http://sre-telegram-relay.monitoring.svc:8080/alertmanager
        send_resolved: true
        http_config:
          authorization:
            type: Bearer
            credentials_file: /etc/alertmanager/secrets/sre-telegram-relay-runtime/alertmanager_auth_token
```

The operator inserts only the bearer material through the approved N100 Secret procedure; no token value is committed.

- [ ] **Step 4: Implement exact-structure validation**

Parse with `yaml.safe_load`; reject non-mapping data, extra root keys except Alertmanager global optional metadata, a root without exactly one child, any nested `routes`, `continue: true`, extra receiver, nonrelay receiver, wrong matcher, wrong URL, wrong credentials file, absent nonempty `group_by`, non-`4h` repeat, or `send_resolved` not true. Return only exit status; errors state a fixed reason name, never config content.

- [ ] **Step 5: Run GREEN and syntax checks**

Run: `python3 -m unittest tests/test_validate_sre_alertmanager_config.py -v`  
Expected: PASS.

Run: `python3 -m py_compile infra/k8s/tools/validate-sre-alertmanager-config.py`  
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add infra/k8s/sre-telegram/alertmanager.yaml.tmpl infra/k8s/tools/validate-sre-alertmanager-config.py tests/test_validate_sre_alertmanager_config.py
git commit -m "feat: Alertmanager SRE 고정 템플릿 추가"
```

## Task 2: preflight와 N100 Secret 절차 단순화

**Files:**
- Modify: `infra/k8s/tools/sre-telegram-preflight.sh`
- Modify: `infra/k8s/tools/sre-telegram-secret-template.sh`
- Modify: `tests/test_k8s_sre_telegram_tools.py`
- Modify: `infra/k8s/README.md`

**Interfaces:**
- Consumes: `--alertmanager-config-file PATH` and Task 1 validator.
- Produces: `check=alertmanager_effective_config status=PASS|FAIL` without config contents.

- [ ] **Step 1: Write failing tool tests**

```python
def test_preflight_runs_amtool_then_fixed_template_validator_without_echoing_config(): ...
def test_preflight_fails_when_validator_rejects_extra_route(): ...
def test_secret_guidance_requires_0600_temporary_file_and_immediate_removal(): ...
```

- [ ] **Step 2: Run RED**

Run: `python3 -m unittest tests/test_k8s_sre_telegram_tools.py -v`  
Expected: new tests FAIL because the current route-tree parser accepts broader forms.

- [ ] **Step 3: Replace route-tree interpretation**

Call `amtool check-config "$file" >/dev/null 2>&1` followed by the Task 1 validator. Delete the in-shell route-tree parser and string contract checks. Missing `amtool`, missing PyYAML, unreadable file, non-0600 operator file, or either nonzero command must fail.

- [ ] **Step 4: Update the operator procedure**

Document: copy template in an N100 private directory, `chmod 600`, insert the approved bearer value locally, run preflight with the path, seed exactly that validated file as `alertmanager.yaml`, then securely remove the temporary file. Do not print a command that echoes a Secret value.

- [ ] **Step 5: Run GREEN**

Run: `python3 -m unittest tests/test_k8s_sre_telegram_tools.py tests/test_validate_sre_alertmanager_config.py -v`  
Expected: PASS.

Run: `bash -n infra/k8s/tools/sre-telegram-preflight.sh infra/k8s/tools/sre-telegram-secret-template.sh`  
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add infra/k8s/tools/sre-telegram-preflight.sh infra/k8s/tools/sre-telegram-secret-template.sh infra/k8s/README.md tests/test_k8s_sre_telegram_tools.py
git commit -m "fix: Alertmanager 고정 템플릿 사전점검 적용"
```

## Task 3: 통합 검증과 N100 적용 승인 게이트

**Files:**
- Test: `tests/test_validate_sre_alertmanager_config.py`, `tests/test_k8s_sre_telegram_tools.py`, existing Telegram relay/manifest/monitoring tests.

- [ ] **Step 1: Run affected local suite**

Run: `python3 -m unittest tests/test_validate_sre_alertmanager_config.py tests/test_k8s_sre_telegram_tools.py tests/test_k8s_sre_telegram_manifests.py tests/test_sre_telegram_relay.py tests/test_k8s_monitoring_tools.py tests/test_k8s_monitoring_values.py -v`  
Expected: all pass.

- [ ] **Step 2: Run static and change-scope checks**

Run: `git diff --check main...HEAD`  
Expected: PASS.

Generate a NUL-delimited changed-file record, then run `python3 scripts/run_change_harness.py --input <record> --input-format git-name-status-z --check-result maintenance=success --agent-context`.  
Expected: `ready_for_review`.

- [ ] **Step 3: Independent review**

Review that fixed-template validation rejects all extra route/receiver variants, the temporary config never reaches terminal output, no Kubernetes Secret `.data` is read, and prohibited files are unchanged.

- [ ] **Step 4: Stop for live approval**

Before any Secret seed, Helm upgrade, K3s apply, or Telegram delivery, request explicit approval. On N100, run preflight with the 0600 temporary config first. Only a PASS result may proceed to seed and `--apply`.

## Self-Review

- Spec section 5.1 maps to Task 1 exact template and Task 2 operator procedure.
- Secret boundary maps to Task 2 and live approval gate in Task 3.
- Existing K3s-only scope and no Compose/startup/scheduler changes are preserved.
- No incomplete marker or unconstrained route behavior remains.
