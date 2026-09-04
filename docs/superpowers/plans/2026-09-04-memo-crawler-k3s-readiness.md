# Crawler·Memo K3s 이행 준비 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `crawler-worker`, `youtube-memo`, `book-memo`의 Compose runtime을 바꾸지 않고 검증 가능한 inactive K3s workload/PVC/Service 계약을 추가한다.

**Architecture:** 서비스마다 replica-zero Deployment, ClusterIP Service, dynamic `local-path` PVC contract를 `.yaml.tmpl`로 분리한다. root Kustomize는 빈 상태를 유지하며 standard-library static test가 Compose의 port·data-path 계약과 inactive 경계를 검사한다.

**Tech Stack:** Kubernetes YAML templates, Kustomize, Python standard-library `unittest`

**Spec:** `docs/superpowers/specs/2026-09-04-memo-crawler-k3s-readiness-design.md`

## Global Constraints

- 실제 K3s resource를 apply하거나 K3s, N100, Compose, Caddy, Cloudflare/tunnel, scheduler, credential, backup, deployment workflow를 변경하지 않는다.
- 새 workload/PVC/Service 파일은 `.yaml.tmpl`, `DRAFT ONLY — NOT FOR APPLY`, draft annotation을 사용한다. `infra/k8s/kustomization.yaml`은 `resources: []`를 유지한다.
- 세 Deployment는 `replicas: 0`; image와 capacity는 `__CONFIRM_*__` placeholder다. Secret 값·key·token·password는 기록하지 않고 Secret 이름만 참조한다.
- Compose 또는 K3s 중 하나만 data writer가 될 수 있다. 이 계획은 writer 전환을 수행하지 않으며 crawler scheduler 소스와 모든 runtime/deployment script를 수정하지 않는다.

---

## File Structure

- Create: `infra/k8s/clusters/n100/apps/{crawler-worker,youtube-memo,book-memo}/deployment.yaml.tmpl` — port, health probe, Secret name, data mount, DB/archive env contract.
- Create: `infra/k8s/clusters/n100/apps/{crawler-worker,youtube-memo,book-memo}/service.yaml.tmpl` — inactive internal HTTP Service.
- Create: `infra/k8s/clusters/n100/apps/{crawler-worker,youtube-memo,book-memo}/kustomization.yaml.tmpl` — example-only local resource list.
- Modify: `infra/k8s/clusters/n100/infra/storage/storage-contract.yaml.tmpl` — three separate RWO `local-path` PVC drafts.
- Create: `tests/test_k8s_memo_crawler_workload_templates.py` — static mapping and non-activation regression tests.
- Modify: `docs/k3s-flux-transition-draft.md` — only reconcile the statement that app-specific inactive templates now exist; retain all operational gates.

### Task 1: Write a failing static contract test

**Files:**

- Create: `tests/test_k8s_memo_crawler_workload_templates.py`

**Interfaces:**

- Consumes: the planned app templates, storage contract, and root Kustomization.
- Produces: `python3 -m unittest tests/test_k8s_memo_crawler_workload_templates.py -v` proof of exact ports, mounts, DB paths, inactive templates, and empty root Kustomization.

- [ ] **Step 1: Write the failing test**

```python
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = {
    "crawler-worker": (8001, "/data/crawler-worker", "NEWS_DB_PATH", "/data/crawler-worker/news_summaries.sqlite3"),
    "youtube-memo": (8002, "/data/youtube-memo", "YOUTUBE_MEMO_DB_PATH", "/data/youtube-memo/youtube_memo.sqlite3"),
    "book-memo": (8003, "/data/book-memo", "BOOK_MEMO_DB_PATH", "/data/book-memo/book_memo.sqlite3"),
}

class WorkloadTemplateTests(unittest.TestCase):
    def test_templates_are_inactive_and_preserve_data_contracts(self):
        for name, (port, mount, env_name, db_path) in SERVICES.items():
            deployment = (ROOT / "infra/k8s/clusters/n100/apps" / name / "deployment.yaml.tmpl").read_text()
            service = (ROOT / "infra/k8s/clusters/n100/apps" / name / "service.yaml.tmpl").read_text()
            self.assertIn("DRAFT ONLY — NOT FOR APPLY", deployment)
            self.assertIn("replicas: 0", deployment)
            self.assertIn(f"containerPort: {port}", deployment)
            self.assertIn("path: /health", deployment)
            self.assertIn(f"mountPath: {mount}", deployment)
            self.assertIn(env_name, deployment)
            self.assertIn(f"value: {db_path}", deployment)
            self.assertIn("__CONFIRM_", deployment)
            self.assertIn("type: ClusterIP", service)
            self.assertIn(f"port: {port}", service)
            self.assertNotIn("NodePort", service)
            self.assertNotIn("hostPath", deployment)
            self.assertNotIn("nodeAffinity", deployment)

    def test_root_kustomization_stays_empty(self):
        root = (ROOT / "infra/k8s/kustomization.yaml").read_text()
        self.assertIn("resources: []", root)
        self.assertNotIn("crawler-worker", root)
        self.assertNotIn("youtube-memo", root)
        self.assertNotIn("book-memo", root)
```

- [ ] **Step 2: Run the test to verify RED**

Run: `python3 -m unittest tests/test_k8s_memo_crawler_workload_templates.py -v`

Expected: FAIL with `FileNotFoundError` because the three template directories are absent.

- [ ] **Step 3: Keep the red test uncommitted until its implementation is green**

Do not create an intentional failing commit.

### Task 2: Implement the crawler inactive Deployment, Service, and PVC contract

**Files:**

- Create: `infra/k8s/clusters/n100/apps/crawler-worker/deployment.yaml.tmpl`
- Create: `infra/k8s/clusters/n100/apps/crawler-worker/service.yaml.tmpl`
- Create: `infra/k8s/clusters/n100/apps/crawler-worker/kustomization.yaml.tmpl`
- Modify: `infra/k8s/clusters/n100/infra/storage/storage-contract.yaml.tmpl`
- Modify: `tests/test_k8s_memo_crawler_workload_templates.py`

**Interfaces:**

- Consumes: port `8001`, `/health`, the existing crawler DB/archive paths, `crawler-worker-runtime`, and `crawler-worker-data-dynamic-draft`.
- Produces: a non-applied RWO `local-path` PVC contract and ClusterIP service; no scheduler or writer transition.

- [ ] **Step 1: Extend the test with crawler-specific assertions**

```python
deployment = (ROOT / "infra/k8s/clusters/n100/apps/crawler-worker/deployment.yaml.tmpl").read_text()
storage = (ROOT / "infra/k8s/clusters/n100/infra/storage/storage-contract.yaml.tmpl").read_text()
self.assertIn("NEWS_ARCHIVE_PATH", deployment)
self.assertIn("value: /data/crawler-worker/news_archive.json", deployment)
self.assertIn("name: crawler-worker-runtime", deployment)
self.assertIn("claimName: crawler-worker-data-dynamic-draft", deployment)
self.assertIn("name: crawler-worker-data-dynamic-draft", storage)
self.assertIn("storage: __CONFIRM_CRAWLER_DATA_CAPACITY__", storage)
```

- [ ] **Step 2: Run the focused test to verify it remains RED**

Run: `python3 -m unittest tests/test_k8s_memo_crawler_workload_templates.py -v`

Expected: FAIL because crawler templates and its PVC definition are absent.

- [ ] **Step 3: Create the minimal crawler Deployment**

```yaml
# DRAFT ONLY — NOT FOR APPLY. Image, data copy, Secret injection, and writer cutover require approval.
spec:
  replicas: 0
  template:
    spec:
      containers:
        - name: crawler-worker
          image: __CONFIRM_CRAWLER_WORKER_IMAGE_REF__
          ports:
            - name: http
              containerPort: 8001
          envFrom:
            - secretRef:
                name: crawler-worker-runtime
                optional: false
          env:
            - name: NEWS_DB_PATH
              value: /data/crawler-worker/news_summaries.sqlite3
            - name: NEWS_ARCHIVE_PATH
              value: /data/crawler-worker/news_archive.json
          volumeMounts:
            - name: crawler-worker-data
              mountPath: /data/crawler-worker
```

Add readiness and liveness probes at `/health` on named port `http`; mount only `crawler-worker-data-dynamic-draft`.

- [ ] **Step 4: Add the ClusterIP Service, example Kustomization, and PVC draft**

```yaml
# service.yaml.tmpl
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: crawler-worker
  ports:
    - name: http
      port: 8001
      targetPort: http
```

```yaml
# storage contract addition
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: crawler-worker-data-dynamic-draft
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: local-path
  resources:
    requests:
      storage: __CONFIRM_CRAWLER_DATA_CAPACITY__
```

The example Kustomization may list only its local deployment and service and must say it is never active.

- [ ] **Step 5: Verify GREEN and commit**

Run: `python3 -m unittest tests/test_k8s_memo_crawler_workload_templates.py tests/test_k8s_storage_draft.py -v`

Expected: PASS.

```bash
git add infra/k8s/clusters/n100/apps/crawler-worker infra/k8s/clusters/n100/infra/storage/storage-contract.yaml.tmpl tests/test_k8s_memo_crawler_workload_templates.py
git commit -m "feat: 크롤러 K3s 비적용 계약 추가"
```

### Task 3: Implement the two memo inactive contracts and reconcile documentation

**Files:**

- Create: `infra/k8s/clusters/n100/apps/youtube-memo/{deployment,service,kustomization}.yaml.tmpl`
- Create: `infra/k8s/clusters/n100/apps/book-memo/{deployment,service,kustomization}.yaml.tmpl`
- Modify: `infra/k8s/clusters/n100/infra/storage/storage-contract.yaml.tmpl`
- Modify: `tests/test_k8s_memo_crawler_workload_templates.py`
- Modify: `docs/k3s-flux-transition-draft.md`

**Interfaces:**

- Consumes: `youtube-memo-runtime` / `book-memo-runtime`, their exact DB paths, ports `8002` / `8003`, and separate PVC names.
- Produces: distinct replica-zero, ClusterIP, RWO `local-path` draft contracts; no public route or Secret value.

- [ ] **Step 1: Extend the test with independent memo Secret and PVC assertions**

```python
storage = (ROOT / "infra/k8s/clusters/n100/infra/storage/storage-contract.yaml.tmpl").read_text()
for name, secret, pvc, capacity in (
    ("youtube-memo", "youtube-memo-runtime", "youtube-memo-data-dynamic-draft", "__CONFIRM_YOUTUBE_DATA_CAPACITY__"),
    ("book-memo", "book-memo-runtime", "book-memo-data-dynamic-draft", "__CONFIRM_BOOK_DATA_CAPACITY__"),
):
    deployment = (ROOT / "infra/k8s/clusters/n100/apps" / name / "deployment.yaml.tmpl").read_text()
    self.assertIn(f"name: {secret}", deployment)
    self.assertIn(f"claimName: {pvc}", deployment)
    self.assertIn(f"name: {pvc}", storage)
    self.assertIn(f"storage: {capacity}", storage)
```

- [ ] **Step 2: Run the focused test to verify RED**

Run: `python3 -m unittest tests/test_k8s_memo_crawler_workload_templates.py -v`

Expected: FAIL because memo templates and PVC drafts are absent.

- [ ] **Step 3: Add exact memo DB mappings**

```yaml
# youtube-memo deployment
- name: YOUTUBE_MEMO_DB_PATH
  value: /data/youtube-memo/youtube_memo.sqlite3
volumeMounts:
  - name: youtube-memo-data
    mountPath: /data/youtube-memo

# book-memo deployment
- name: BOOK_MEMO_DB_PATH
  value: /data/book-memo/book_memo.sqlite3
volumeMounts:
  - name: book-memo-data
    mountPath: /data/book-memo
```

For each Deployment add `replicas: 0`, a service-specific immutable-image placeholder, `envFrom.secretRef`, named HTTP port (`8002` or `8003`), both `/health` probes, its matching claim, and draft annotation. Create matching ClusterIP Service and example-only Kustomization. Add RWO local-path PVCs with `__CONFIRM_YOUTUBE_DATA_CAPACITY__` and `__CONFIRM_BOOK_DATA_CAPACITY__`; do not add NodePort, Ingress, limits, affinity, host path, or a concrete digest.

- [ ] **Step 4: Correct only the stale transition-draft repository-state sentence**

State that the three services have replica-zero, non-applied Deployment/ClusterIP/PVC contracts. Preserve explicit gates for data copy, ownership, backup/restore, Secret injection, crawler scheduler behavior, Caddy, and one-writer cutover.

- [ ] **Step 5: Verify GREEN, scope, and commit**

Run:

```bash
python3 -m unittest tests/test_k8s_memo_crawler_workload_templates.py tests/test_k8s_storage_draft.py tests/crawler_worker/test_news_scheduler.py tests/youtube_memo/test_video_titles.py tests/book_memo/test_book_service.py -v
git diff --check
git diff --name-status -z --find-renames HEAD > /tmp/memo-crawler-k3s-readiness.name-status.z
python3 scripts/run_change_harness.py --input /tmp/memo-crawler-k3s-readiness.name-status.z --input-format git-name-status-z --check-result maintenance=success --agent-context
```

Expected: all tests and whitespace check PASS; harness reports `ready_for_review` with `maintenance` because the files are infrastructure, tests, and documentation only.

```bash
git add infra/k8s/clusters/n100/apps/youtube-memo infra/k8s/clusters/n100/apps/book-memo infra/k8s/clusters/n100/infra/storage/storage-contract.yaml.tmpl tests/test_k8s_memo_crawler_workload_templates.py docs/k3s-flux-transition-draft.md
git commit -m "feat: 메모 K3s 비적용 계약 추가"
```

## Execution Boundary

This plan ends at locally tested, inactive repository contracts. It does not authorize image build/publish, digest or capacity selection, backup/restore, Secret creation, PVC binding, scale-up, Compose stop/start, runner installation, scheduler operation, NodePort/Caddy/tunnel changes, or public cutover. Each remains a separately approved operational task.
