# P1 Daily SLO Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store bounded, validated daily SLO evidence for 30-day monthly review without extending Prometheus retention.

**Architecture:** A non-root K3s CronJob calls a small Python collector which queries the existing internal Prometheus service and the public Portal health endpoint. It replaces one KST-day record in a single ConfigMap, keeps the newest 30 records, and records missing inputs as `unobservable`. The existing monthly audit receives read-only access and adds only aggregate evidence coverage to its status result.

**Tech Stack:** Python 3 standard library, Bash, Kubernetes YAML, unittest, PyYAML.

**Spec:** `docs/superpowers/specs/2026-09-15-p1-daily-slo-evidence-design.md`

## Global Constraints

- Do not modify Prometheus retention, Portal PVC, Secret, operational data, Caddy, or Tunnel ingress.
- Use only `monitoring/slo-daily-evidence` ConfigMap; retain at most 30 date-unique records.
- Treat missing, invalid, or failed Prometheus inputs as `unobservable`, never as success.
- The CronJob runs at 02:15 KST with `Forbid`, no retries, non-root, read-only root filesystem, no Secret/PVC/host mounts, and all capabilities dropped.
- The monthly audit may read evidence but must not mutate it; evidence does not block deployment or trigger recovery.

---

### Task 1: Daily-record validation and bounded merge library

**Files:**
- Create: `infra/k8s/tools/slo-daily-evidence.py`
- Create: `tests/test_k8s_slo_daily_evidence.py`

**Interfaces:**
- Produces `validate_record(record: dict) -> dict` and `merge_records(existing: list, record: dict) -> list`.
- `merge_records` replaces the matching `date`, sorts by descending KST date, and returns at most 30 records.

- [ ] **Step 1: Write failing tests**

```python
def test_merge_replaces_same_date_and_keeps_thirty_newest(self):
    records = [{"date": f"2026-08-{day:02d}", **valid_fields()} for day in range(1, 31)]
    result = module.merge_records(records, {"date": "2026-08-30", **valid_fields(overall="unobservable", missing=["portal_ready"])})
    self.assertEqual(len(result), 30)
    self.assertEqual(result[0]["overall"], "unobservable")
    self.assertNotIn("2026-08-01", [item["date"] for item in result])
```

- [ ] **Step 2: Run the test and confirm failure**

Run: `python3 -m unittest tests.test_k8s_slo_daily_evidence -v`

Expected: failure because the collector module does not exist.

- [ ] **Step 3: Implement the library**

```python
def merge_records(existing, record):
    merged = [item for item in existing if item["date"] != record["date"]]
    merged.append(validate_record(record))
    return sorted(merged, key=lambda item: item["date"], reverse=True)[:30]
```

Implement strict UTC timestamp, enum, numeric, `missing`, and nullable Portal HTTP validation with only Python standard-library modules.

- [ ] **Step 4: Run the focused test**

Run: `python3 -m unittest tests.test_k8s_slo_daily_evidence -v`

Expected: PASS.

### Task 2: Prometheus/public-health collection and fail-closed ConfigMap update

**Files:**
- Modify: `infra/k8s/tools/slo-daily-evidence.py`
- Modify: `tests/test_k8s_slo_daily_evidence.py`

**Interfaces:**
- Consumes `PROMETHEUS_URL`, `EVIDENCE_CONFIGMAP`, and in-cluster `kubectl` credentials.
- Produces one `records.json` payload through `kubectl patch configmap` only after validation.

- [ ] **Step 1: Write failing tests**

```python
def test_collector_records_missing_prometheus_metric_as_unobservable(self):
    result = run_collector(prometheus={"portal_http": None}, public_status=200)
    self.assertEqual(result.record["overall"], "unobservable")
    self.assertIn("portal_http", result.record["missing"])
    self.assertEqual(result.record["public_health"], "ok")
```

- [ ] **Step 2: Run the test and confirm failure**

Run: `python3 -m unittest tests.test_k8s_slo_daily_evidence.SloDailyEvidenceTests.test_collector_records_missing_prometheus_metric_as_unobservable -v`

Expected: failure because collection has not been implemented.

- [ ] **Step 3: Implement collection**

Query only the existing Portal HTTP counters/histogram, Portal Ready metric, and crawler freshness metrics. Parse Prometheus JSON defensively; query failure or absent result adds the source to `missing`. Use `urllib.request` with bounded timeouts for the public health probe. Preserve `failed` for non-200 public health and reserve `unobservable` for collection failure.

- [ ] **Step 4: Run focused tests**

Run: `python3 -m unittest tests.test_k8s_slo_daily_evidence -v`

Expected: PASS for normal, invalid-number, missing-source, duplicate-date, and 30-record boundary cases.

### Task 3: Least-privilege daily CronJob and image

**Files:**
- Create: `infra/k8s/slo-evidence/Dockerfile`
- Create: `infra/k8s/slo-evidence/slo-daily-evidence-cronjob.yaml`
- Modify: `tests/test_k8s_slo_daily_evidence.py`
- Modify: `infra/k8s/README.md`

**Interfaces:**
- Produces `ServiceAccount/slo-daily-evidence`, fixed ConfigMap RBAC, and `CronJob/slo-daily-evidence` in `monitoring`.

- [ ] **Step 1: Write failing manifest test**

```python
def test_daily_cronjob_is_bounded_and_has_no_sensitive_storage(self):
    job = find("CronJob", "slo-daily-evidence", "monitoring")
    self.assertEqual(job["spec"]["schedule"], "15 2 * * *")
    self.assertEqual(job["spec"]["timeZone"], "Asia/Seoul")
    self.assertEqual(job["spec"]["concurrencyPolicy"], "Forbid")
    self.assertNotIn("secret", str(job).lower())
    self.assertNotIn("persistentVolumeClaim", str(job))
```

- [ ] **Step 2: Run and confirm failure**

Run: `python3 -m unittest tests.test_k8s_slo_daily_evidence -v`

Expected: failure because the manifest does not exist.

- [ ] **Step 3: Add image and manifest**

Copy only `slo-daily-evidence.py` into a Python slim non-root image. Declare ConfigMap with `records.json: []`, explicit Role `get,patch` for that resource name, and a suspended-by-default CronJob using the image with `imagePullPolicy: Never`.

- [ ] **Step 4: Run manifest tests**

Run: `python3 -m unittest tests.test_k8s_slo_daily_evidence -v`

Expected: PASS.

### Task 4: Read-only monthly aggregate and operational verification

**Files:**
- Modify: `infra/k8s/sre-audit-automation/quarterly-sre-audit-cronjob.yaml`
- Modify: `infra/k8s/tools/quarterly-sre-audit-runner.sh`
- Modify: `tests/test_k8s_quarterly_sre_audit_cronjob.py`
- Modify: `tests/test_k8s_quarterly_sre_audit_automation.py`
- Modify: `infra/k8s/README.md`

**Interfaces:**
- Consumes `monitoring/slo-daily-evidence` with read-only RBAC.
- Produces `slo_evidence`, `slo_days_recorded`, `slo_days_ok`, and `slo_days_unobservable` status fields.

- [ ] **Step 1: Write failing audit tests**

```python
self.assertIn({"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["slo-daily-evidence"], "verbs": ["get"]}, status_read_rules)
self.assertIn('"slo_evidence":"%s"', runner_source)
```

- [ ] **Step 2: Run and confirm failure**

Run: `python3 -m unittest tests.test_k8s_quarterly_sre_audit_cronjob -v`

Expected: failure because the monthly runner has no SLO evidence contract.

- [ ] **Step 3: Implement read-only aggregate**

Parse `records.json` with Python, reject malformed arrays or invalid records, calculate the three day counts, and report `unobservable` for fewer than 30 records or any unobservable day. Keep existing monthly audit checks and relay fields unchanged; do not patch the evidence ConfigMap.

- [ ] **Step 4: Run relevant checks and document activation order**

Run: `python3 -m unittest tests.test_k8s_slo_daily_evidence tests.test_k8s_quarterly_sre_audit_cronjob tests.test_k8s_quarterly_sre_audit_automation tests.test_documentation_index -v`

Expected: PASS.

Document: build/import the image, apply the manifest suspended, run one manual Job, validate ConfigMap shape without printing values, then unsuspend only after success. N100 apply requires separate user approval.

### Task 5: Final verification and integration

**Files:**
- Modify: `tests/ci_test_matrix.json`

- [ ] **Step 1: Add the new SLO test module to the K8s contracts CI command**

```json
"python3 -m unittest ... tests.test_k8s_slo_daily_evidence ..."
```

- [ ] **Step 2: Run change harness and CI-equivalent suites**

Run: `python3 scripts/run_change_harness.py --input <git-name-status-z> --input-format git-name-status-z --agent-context`

Run: `python3 tests/run_service_tests.py --suite k8s-contracts`

Run: `python3 tests/run_service_tests.py --suite maintenance`

- [ ] **Step 3: Commit implementation**

```bash
git add infra/k8s/slo-evidence infra/k8s/sre-audit-automation infra/k8s/tools tests docs
git commit -m "feat: 30일 SLO 일별 증적 수집 추가"
```

## Plan self-review

- Spec coverage: Tasks 1-3 implement bounded daily evidence, Task 4 implements read-only monthly aggregation, and Task 5 covers CI and integration.
- Placeholder scan: no deferred implementation items are present.
- Interface consistency: `records.json`, `slo-daily-evidence`, and the four monthly status fields use the same names in all tasks.
