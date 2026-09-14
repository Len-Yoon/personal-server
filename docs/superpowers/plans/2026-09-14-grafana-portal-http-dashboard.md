# Portal HTTP Grafana Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Portal HTTP Prometheus 메트릭을 Grafana 대시보드로 안전하게 적용·확인·롤백할 수 있게 함.

**Architecture:** Grafana sidecar가 감지하는 `grafana_dashboard=1` ConfigMap에 고정된 4개 PromQL 패널을 저장함. 별도 운영 도구는 Grafana Deployment 준비 상태를 검사한 뒤 그 ConfigMap만 apply 또는 delete하며, monitoring 설치와 Portal runtime을 변경하지 않음.

**Tech Stack:** Kubernetes ConfigMap, Grafana dashboard JSON, Prometheus PromQL, Bash, Python unittest.

**Spec:** `docs/superpowers/specs/2026-09-14-grafana-portal-http-dashboard-design.md`

## Global Constraints

- Secret 값, PVC, Caddy, Cloudflare Tunnel, Portal runtime 설정을 읽거나 변경하지 않음.
- 적용·롤백 대상은 `monitoring/portal-http-observability` ConfigMap 하나로 제한함.
- `--check`, `--apply`, `--rollback`은 모두 명시적으로 지정해야 하며, 인자 없는 실행은 usage 오류로 종료함.

---

### Task 1: Dashboard ConfigMap 계약

**Files:**
- Create: `infra/k8s/monitoring/portal-http-dashboard.yaml`
- Test: `tests/test_k8s_grafana_portal_http_dashboard.py`

**Interfaces:**
- Produces: `monitoring/portal-http-observability` ConfigMap, key `portal-http-observability.json`, label `grafana_dashboard=1`.

- [ ] **Step 1: Write the failing dashboard contract test**

```python
self.assertTrue(DASHBOARD.is_file())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_k8s_grafana_portal_http_dashboard`

Expected: `FAIL` because the dashboard manifest does not exist.

- [ ] **Step 3: Add the minimal ConfigMap**

```yaml
metadata:
  name: portal-http-observability
  namespace: monitoring
  labels:
    grafana_dashboard: "1"
```

Include exactly four panels with `portal_http_requests_total` and `portal_http_request_duration_seconds_bucket` queries from the spec.

- [ ] **Step 4: Run dashboard and monitoring contracts**

Run: `python3 -m unittest tests.test_k8s_grafana_portal_http_dashboard tests.test_k8s_monitoring_tools tests.test_k8s_monitoring_values`

Expected: PASS.

### Task 2: Explicit dashboard apply and rollback boundary

**Files:**
- Create: `infra/k8s/tools/monitoring-dashboard-apply.sh`
- Modify: `tests/test_k8s_monitoring_tools.py`

**Interfaces:**
- Consumes: `infra/k8s/monitoring/portal-http-dashboard.yaml`.
- Produces: `monitoring_dashboard=PASS|FAIL` and supports `--check`, `--apply`, `--rollback`.

- [ ] **Step 1: Write failing tool behavior tests**

```python
result, calls = self.run_tool("monitoring-dashboard-apply.sh", "--apply", stubs=stubs)
self.assertIn("apply -f", calls)
self.assertNotIn("secret", calls)
```

Add equivalent assertions that default mode is read-only and rollback only deletes `configmap/portal-http-observability`.

- [ ] **Step 2: Run the focused tool tests to verify failure**

Run: `python3 -m unittest tests.test_k8s_monitoring_tools.MonitoringToolsTest.test_dashboard_apply_requires_explicit_mode`

Expected: FAIL because the tool does not exist.

- [ ] **Step 3: Implement a fail-closed Bash tool**

```bash
case "$mode" in
  --check) verify_dashboard_contract ;;
  --apply) verify_dashboard_contract && sudo k3s kubectl apply -f "$DASHBOARD" ;;
  --rollback) sudo k3s kubectl -n monitoring delete configmap portal-http-observability --ignore-not-found ;;
esac
```

Make `verify_dashboard_contract` require the Grafana Deployment and dashboard ConfigMap label, without reading Secret values.

- [ ] **Step 4: Run tool contracts**

Run: `python3 -m unittest tests.test_k8s_monitoring_tools tests.test_k8s_grafana_portal_http_dashboard`

Expected: PASS.

### Task 3: Operator procedure and final verification

**Files:**
- Modify: `infra/k8s/README.md`

**Interfaces:**
- Consumes: `monitoring-dashboard-apply.sh --check|--apply|--rollback`.
- Produces: copyable apply, Grafana UI confirmation, and rollback instructions.

- [ ] **Step 1: Document only the scoped operational commands**

```bash
bash infra/k8s/tools/monitoring-dashboard-apply.sh --check
bash infra/k8s/tools/monitoring-dashboard-apply.sh --apply
bash infra/k8s/tools/monitoring-dashboard-apply.sh --rollback
```

State that the tool changes only the dashboard ConfigMap and that Grafana UI confirmation is required after apply.

- [ ] **Step 2: Run final verification**

Run: `python3 -m unittest tests.test_k8s_monitoring_tools tests.test_k8s_monitoring_values tests.test_k8s_grafana_portal_http_dashboard && git diff --check`

Expected: PASS.
