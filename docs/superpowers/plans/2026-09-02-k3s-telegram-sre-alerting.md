# K3s Telegram SRE 알림 1차 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telegram `/상태` 조회와 K3s 장애·복구 알림을 제공하되 기존 Compose·서버 기동·스케줄러는 변경하지 않음.

**Architecture:** `monitoring` namespace에 단일 복제본 `sre-telegram-relay`를 배포함. relay는 Telegram long polling으로 허용 chat의 `/상태`만 받고, Alertmanager는 ClusterIP webhook으로 firing·resolved 이벤트를 relay에 전달함. relay는 최소 RBAC K3s API와 Prometheus HTTP API만 읽음.

**Tech Stack:** K3s, kube-prometheus-stack 88.6.1, Alertmanager, PrometheusRule, Kubernetes RBAC/Secret/ConfigMap, Python 3.11 표준 라이브러리, Bash, unittest.

**Spec:** `docs/superpowers/specs/2026-09-02-k3s-telegram-sre-alerting-design.md`

## Global Constraints

- Compose, Portal, Caddy, 외부 Ingress·NodePort·LoadBalancer, 서버 기동 스크립트, Windows bootstrap, 기존 스케줄러를 수정하지 않음.
- Grafana와 Prometheus는 ClusterIP 내부 전용으로 유지함.
- Telegram token, 허용 chat ID, Alertmanager 인증 token은 Git·문서·테스트 출력에 기록하지 않음.
- relay는 Secret 읽기, Pod exec/delete, Deployment patch 권한을 받지 않음.
- 설치 도구는 기본 render 또는 읽기 전용이며 `--apply`에서만 cluster 상태를 변경함.

---

## File Structure

| 파일 | 책임 |
|---|---|
| `sre-telegram-relay/app/main.py` | Telegram long polling, `/상태`, Alertmanager webhook, health endpoint |
| `sre-telegram-relay/Dockerfile` | Secret을 포함하지 않는 relay image |
| `infra/k8s/sre-telegram/base.yaml` | ServiceAccount, 최소 RBAC, Deployment, ClusterIP Service, state ConfigMap |
| `infra/k8s/sre-telegram/prometheus-rule.yaml` | 재시작·Deployment·PVC·target 경고 |
| `infra/k8s/sre-telegram/alertmanager-values.yaml` | Alertmanager 활성화와 config Secret 참조 계약 |
| `infra/k8s/tools/sre-telegram-*.sh` | preflight, Secret 계약 안내, install, verify |
| `tests/test_sre_telegram_relay.py` | 명령 인증·상태·webhook 단위 테스트 |
| `tests/test_k8s_sre_telegram_manifests.py` | RBAC·ClusterIP·경고·Secret 참조 계약 테스트 |
| `tests/test_k8s_sre_telegram_tools.py` | 도구의 fail-closed·명시적 apply·금지 동작 테스트 |

## Task 1: Relay 순수 로직과 HTTP 경계

**Files:**
- Create: `sre-telegram-relay/app/main.py`
- Create: `sre-telegram-relay/Dockerfile`
- Create: `tests/test_sre_telegram_relay.py`

**Interfaces:**
- Consumes: `TELEGRAM_BOT_TOKEN_FILE`, `ALLOWED_CHAT_ID_FILE`, `ALERTMANAGER_AUTH_TOKEN_FILE`과 표준 in-cluster service-account token/CA 파일.
- Produces: `build_status_summary(k8s_client, prometheus_client) -> str`, `handle_update(update: dict) -> str | None`, `handle_alert(payload: dict, authorization: str) -> tuple[int, str]`, `/healthz` HTTP 200.

- [ ] **Step 1: Write the failing authorization tests**

```python
def test_allowed_status_command_returns_redacted_summary():
    relay = RelayService(allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())
    assert relay.handle_update({"message": {"chat": {"id": 123}, "text": "/상태"}}).startswith("[K3s 상태]")

def test_other_chat_never_receives_status():
    relay = RelayService(allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())
    assert relay.handle_update({"message": {"chat": {"id": 999}, "text": "/상태"}}) is None

def test_alert_webhook_rejects_wrong_bearer_token():
    relay = RelayService(alertmanager_auth_token="expected", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())
    assert relay.handle_alert({"status": "firing", "alerts": []}, "Bearer wrong")[0] == 401
```

- [ ] **Step 2: Run RED**

Run: `python3 -m unittest tests/test_sre_telegram_relay.py -v`  
Expected: FAIL because the relay does not exist.

- [ ] **Step 3: Implement the smallest relay**

Implement `RelayService`, `KubernetesClient` and `PrometheusClient`. `/상태` reads nodes, Pods, Deployments, PVCs and active Prometheus targets only; it returns aggregate counts and limited failure reasons. Alertmanager accepts only the configured bearer token, formats firing/resolved messages, and does not execute remediation.

- [ ] **Step 4: Add edge-case tests**

Test unsupported commands, K3s/Prometheus read failure, firing/resolved formatting, no token/chat ID in replies, and no duplicate Telegram update after restart offset persistence.

- [ ] **Step 5: Run GREEN and build-only image check**

Run: `python3 -m unittest tests/test_sre_telegram_relay.py -v`  
Expected: PASS.

Run: `docker build -t personal-server-sre-telegram-relay:test sre-telegram-relay`  
Expected: image build succeeds without Secret values.

- [ ] **Step 6: Commit**

Run: `git add sre-telegram-relay tests/test_sre_telegram_relay.py && git commit -m "feat: Telegram SRE relay 추가"`

## Task 2: K3s manifest·RBAC·경고 계약

**Files:**
- Create: `infra/k8s/sre-telegram/base.yaml`
- Create: `infra/k8s/sre-telegram/prometheus-rule.yaml`
- Create: `infra/k8s/sre-telegram/alertmanager-values.yaml`
- Create: `tests/test_k8s_sre_telegram_manifests.py`

**Interfaces:**
- Consumes: relay image; externally seeded Secrets `sre-telegram-relay-runtime`, `sre-telegram-alertmanager-config`.
- Produces: relay ClusterIP Service, ServiceAccount `sre-telegram-relay`, minimum Role/RoleBinding, PrometheusRule `sre-telegram-k3s-alerts`.

- [ ] **Step 1: Write failing manifest contract tests**

```python
def test_relay_service_is_cluster_ip_without_published_node_port(): ...
def test_role_is_read_only_except_relay_state_configmap_update(): ...
def test_rules_cover_restart_deployment_pvc_and_target_failures(): ...
def test_alertmanager_references_existing_secret_not_inline_token(): ...
```

- [ ] **Step 2: Run RED**

Run: `python3 -m unittest tests/test_k8s_sre_telegram_manifests.py -v`  
Expected: FAIL because manifests do not exist.

- [ ] **Step 3: Implement base manifest and minimum Role**

Create one non-root relay replica with dropped capabilities, read-only root filesystem, `/healthz` readiness/liveness, `imagePullPolicy: Never`, Secret key references, ClusterIP webhook Service, and a state ConfigMap. The Role has only get/list/watch for nodes, Pods, Deployments and PVCs, plus get/update/patch for the named state ConfigMap.

- [ ] **Step 4: Implement alert rules and Alertmanager values**

Create four rules: restart increase over 15 minutes, unavailable Deployment for 10 minutes, unbound PVC for 10 minutes, target down for 5 minutes. Route only `sre_telegram: "true"` alerts to the relay, group by alert and resource labels, repeat after 4 hours, and set `send_resolved: true`. Alertmanager config must be seeded as a Kubernetes Secret and only referenced by name.

- [ ] **Step 5: Run GREEN and Helm render**

Run: `python3 -m unittest tests/test_k8s_sre_telegram_manifests.py -v`  
Expected: PASS.

Run: `helm template personal-server-monitoring prometheus-community/kube-prometheus-stack --namespace monitoring --version 88.6.1 --values infra/k8s/monitoring/values.n100.yaml --values infra/k8s/sre-telegram/alertmanager-values.yaml >/dev/null`  
Expected: exit 0.

- [ ] **Step 6: Commit**

Run: `git add infra/k8s/sre-telegram tests/test_k8s_sre_telegram_manifests.py && git commit -m "feat: K3s Telegram SRE 경고 리소스 추가"`

## Task 3: N100 안전 설치·검증 도구

**Files:**
- Create: `infra/k8s/tools/sre-telegram-preflight.sh`
- Create: `infra/k8s/tools/sre-telegram-secret-template.sh`
- Create: `infra/k8s/tools/sre-telegram-install.sh`
- Create: `infra/k8s/tools/sre-telegram-verify.sh`
- Create: `tests/test_k8s_sre_telegram_tools.py`
- Modify: `infra/k8s/README.md`

**Interfaces:**
- Consumes: Tasks 1–2 outputs and existing monitoring release.
- Produces: final lines `sre_telegram_preflight=PASS|FAIL`, `sre_telegram_install=PASS|FAIL`, `sre_telegram_verify=PASS|FAIL`.

- [ ] **Step 1: Write failing tool tests**

```python
def test_preflight_invokes_only_read_only_k3s_and_helm_commands(): ...
def test_render_never_imports_image_or_applies_resources(): ...
def test_apply_requires_all_secret_keys_by_name_without_reading_values(): ...
def test_verify_never_reads_or_prints_secret_data(): ...
```

- [ ] **Step 2: Run RED**

Run: `python3 -m unittest tests/test_k8s_sre_telegram_tools.py -v`  
Expected: FAIL because tools do not exist.

- [ ] **Step 3: Implement preflight and Secret contract guidance**

Preflight checks only K3s Ready, monitoring release, Prometheus/Grafana health, image build prerequisites and Secret name/key presence. The Secret guidance prints required key names and N100-local manual seed procedure only; it never creates or displays values.

- [ ] **Step 4: Implement render/apply/verify**

`--render` performs Helm template and Kubernetes client dry-run only. `--apply` requires seeded Secret objects, builds and imports the local Docker image into `k3s ctr -n k8s.io`, upgrades the existing monitoring release with the Alertmanager values, and applies relay resources. On failure it deletes only relay resources created by this tool. Verify checks relay Ready, ClusterIP, PrometheusRule, non-escalated RBAC, temporary localhost `/healthz`, and Prometheus target health.

- [ ] **Step 5: Run GREEN and shell syntax checks**

Run: `python3 -m unittest tests/test_k8s_sre_telegram_tools.py -v`  
Expected: PASS.

Run: `bash -n infra/k8s/tools/sre-telegram-preflight.sh infra/k8s/tools/sre-telegram-secret-template.sh infra/k8s/tools/sre-telegram-install.sh infra/k8s/tools/sre-telegram-verify.sh`  
Expected: exit 0.

- [ ] **Step 6: Commit**

Run: `git add infra/k8s/tools/sre-telegram-*.sh infra/k8s/README.md tests/test_k8s_sre_telegram_tools.py && git commit -m "feat: Telegram SRE 설치 및 검증 도구 추가"`

## Task 4: 통합 검증과 N100 승인 게이트

**Files:**
- Test: `tests/test_sre_telegram_relay.py`, `tests/test_k8s_sre_telegram_manifests.py`, `tests/test_k8s_sre_telegram_tools.py`, existing monitoring/SRE tests.

**Interfaces:**
- Consumes: Tasks 1–3 outputs.
- Produces: local and N100 verification evidence with no Secret values.

- [ ] **Step 1: Run complete affected suite**

Run: `python3 -m unittest tests/test_sre_telegram_relay.py tests/test_k8s_sre_telegram_manifests.py tests/test_k8s_sre_telegram_tools.py tests/test_k8s_monitoring_tools.py tests/test_k8s_monitoring_values.py tests/test_k8s_sre_health_audit.py tests/test_k8s_sre_pod_recovery_lab.py -v`  
Expected: all tests pass.

- [ ] **Step 2: Run static, render and change-scope checks**

Run: `git diff --check main...HEAD`  
Expected: exit 0.

Generate the NUL-delimited changed-file record and run `python3 scripts/run_change_harness.py --input <record> --input-format git-name-status-z --check-result maintenance=success --agent-context`.  
Expected: `ready_for_review`.

- [ ] **Step 3: Independent review**

Review no Secret values, least-privilege RBAC, no Compose/startup/scheduler/Caddy changes, Alertmanager firing/resolved route isolation, and rollback scope.

- [ ] **Step 4: Stop for external-delivery approval**

Run N100 preflight and render only. Before creating Telegram/Alertmanager Secrets or running `--apply`, request explicit approval because this creates external Telegram delivery and a running relay.

- [ ] **Step 5: Live test after approval**

Run installation and verify; test one allowed `/상태` request and one controlled firing/resolved test alert. Do not induce an actual service failure. Confirm no Secret value appears in terminal or GitHub logs.

## Self-Review

- Tasks 1–2 cover Telegram command, alert delivery, recovery message, minimum RBAC and Secret boundaries.
- Task 3 covers explicit install, rollback and verification without changing prohibited areas.
- Task 4 covers all affected tests, independent review and the external-delivery approval gate.
- No `TODO`, `TBD` or unbounded implementation step remains.
