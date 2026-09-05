# N100 안전 자동 배포 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CI가 검증한 `main` revision만 N100에 자동 배포하고, allowlisted Compose 서비스의 health 실패 시 직전 정상 revision으로 한 번 복구함.

**Architecture:** GitHub Actions의 변경 분류 job은 정확한 CI `head_sha`에서 안전한 Compose 서비스만 선택함. N100 runner의 새 배포 스크립트는 해당 SHA가 `origin/main`의 조상인지 확인하고 detached checkout으로 배포하며, 성공 revision만 runner 전용 state 파일에 기록함. 새 revision health 실패 시 state 파일의 직전 revision을 한 번 재배포·검증하고 반복 rollback은 금지함.

**Tech Stack:** GitHub Actions workflow_run, Windows self-hosted runner, WSL Bash, Docker Compose, Python 3.11 unittest.

**Spec:** `docs/superpowers/specs/2026-09-05-safe-n100-continuous-deployment-design.md`

## Global Constraints

- 자동 대상은 `crawler-worker`, `youtube-memo`, `book-memo`, `car-care-worker` Compose 서비스만 허용함.
- Portal·Caddy·system-agent·homeops-executor·K3s·Secret·PVC·backup·scheduler는 1차 CD에서 자동 적용하지 않음.
- CI `workflow_run.head_sha`만 배포하고, `origin/main` 최신 revision으로 대체하지 않음.
- `.env`, `data/`, Secret, credential, PVC 데이터를 읽어 출력하거나 변경하지 않음.
- health 성공 뒤에만 마지막 정상 revision을 기록하고, 실패 시 rollback은 한 번만 수행함.
- 기존 `scripts/deploy-n100.sh` 수동/기존 배포 경로는 변경하지 않음.

---

### Task 1: 정책과 변경 분류 계약

**Files:**
- Modify: `AGENTS.md`
- Create: `scripts/classify-n100-safe-deployment.py`
- Create: `tests/test_n100_safe_deployment.py`

**Interfaces:**
- Consumes: `classify_changed_paths(paths: Iterable[str]) -> DeploymentDecision`
- Produces: `DeploymentDecision(action: Literal["skip", "deploy", "blocked"], services: tuple[str, ...], reason: str)` and CLI `--base <revision> --head <revision> --github-output <path>`.

- [ ] **Step 1: Write failing classifier tests**

```python
def test_allowlisted_service_change_deploys_only_that_service(self):
    decision = classifier.classify_changed_paths(["crawler-worker/app/main.py"])
    self.assertEqual(decision.action, "deploy")
    self.assertEqual(decision.services, ("crawler-worker",))

def test_portal_or_k3s_change_is_blocked_before_deployment(self):
    decision = classifier.classify_changed_paths(["portal-web/app/main.py"])
    self.assertEqual(decision.action, "blocked")
```

- [ ] **Step 2: Run tests to verify RED**

Run: `PYTHONPATH=. python3 -m unittest tests.test_n100_safe_deployment`

Expected: FAIL because `scripts/classify-n100-safe-deployment.py` does not exist.

- [ ] **Step 3: Add the narrow CD policy exception and classifier**

```python
SAFE_SERVICE_PREFIXES = {
    "crawler-worker/": "crawler-worker",
    "youtube-memo/": "youtube-memo",
    "book-memo/": "book-memo",
    "car-care-worker/": "car-care-worker",
}
BLOCKED_PREFIXES = (
    "portal-web/", "caddy/", "system-agent/", "homeops-executor/", "infra/k8s/",
)
```

Add a narrowly worded `AGENTS.md` CD exception that permits only the new classifier, safe deploy/health scripts, and `deploy-n100.yml`; explicitly exclude Portal, K3s, Secrets, PVC/data, Caddy, server bootstrap, and scheduler.

- [ ] **Step 4: Run classifier tests to verify GREEN**

Run: `PYTHONPATH=. python3 -m unittest tests.test_n100_safe_deployment`

Expected: PASS for allowlisted, shared Compose, mixed, blocked, and documentation-only cases.

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md scripts/classify-n100-safe-deployment.py tests/test_n100_safe_deployment.py
git commit -m "feat: 안전 자동 배포 변경 분류 추가"
```

### Task 2: revision-pinned safe Compose deployment and one rollback

**Files:**
- Create: `scripts/deploy-n100-safe.sh`
- Create: `scripts/verify-n100-safe-deployment-health.sh`
- Modify: `tests/test_n100_safe_deployment.py`

**Interfaces:**
- Consumes: `deploy-n100-safe.sh <expected-sha> <service>...`, `$N100_SAFE_DEPLOY_STATE_DIR`, `$N100_SAFE_DEPLOY_PROJECT_ROOT`
- Produces: exit 0 only after requested service health passes; state file `last-healthy-revision`; machine-readable stderr stages `safe_cd_stage=preflight|deploy|health|rollback`.

- [ ] **Step 1: Write failing deployment tests**

```python
def test_deploy_refuses_revision_that_is_not_an_origin_main_ancestor(self):
    result, calls = self.run_safe_deploy(expected_sha="f" * 40)
    self.assertNotEqual(result.returncode, 0)
    self.assertNotIn("docker compose", calls)

def test_health_failure_rolls_back_once_to_saved_healthy_revision(self):
    result, calls = self.run_safe_deploy(
        expected_sha=NEW_SHA, previous_sha=OLD_SHA, health_results=[1, 0]
    )
    self.assertEqual(result.returncode, 0)
    self.assertEqual(calls.count("checkout --detach"), 2)
    self.assertIn(OLD_SHA, calls)
```

- [ ] **Step 2: Run tests to verify RED**

Run: `PYTHONPATH=. python3 -m unittest tests.test_n100_safe_deployment`

Expected: FAIL because safe deploy and health scripts do not exist.

- [ ] **Step 3: Implement deploy and health scripts**

```bash
git fetch --prune origin
git merge-base --is-ancestor "$expected_sha" origin/main || exit 1
git checkout --detach "$expected_sha"
docker compose -f docker-compose.yml -f docker-compose.n100.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build --no-deps "$@"
```

The health script must inspect only the requested allowlisted containers and call only their loopback health endpoints. The deploy script writes the state file only after success. On the first health failure, it reads a validated 40-hex saved revision, checks it is an `origin/main` ancestor, performs one rollback deploy and health check, then returns the rollback outcome. It must not run `git reset --hard`, `rm`, `kubectl`, `sudo`, or touch `.env`/`data`.

- [ ] **Step 4: Run deployment tests to verify GREEN**

Run: `PYTHONPATH=. python3 -m unittest tests.test_n100_safe_deployment`

Expected: PASS for revision mismatch, successful record, first-deploy failure, rollback success, rollback failure, invalid state, and service allowlist rejection.

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy-n100-safe.sh scripts/verify-n100-safe-deployment-health.sh tests/test_n100_safe_deployment.py
git commit -m "feat: revision 고정 안전 자동 배포 추가"
```

### Task 3: CI-success-to-N100 workflow integration

**Files:**
- Modify: `.github/workflows/deploy-n100.yml`
- Modify: `tests/test_deploy_n100.py`
- Modify: `tests/test_n100_safe_deployment.py`

**Interfaces:**
- Consumes: classifier GitHub output keys `action`, `services`, `reason`; `github.event.workflow_run.head_sha`.
- Produces: N100 runner invocation `bash ./scripts/deploy-n100-safe.sh <head_sha> <services...>` only for `action=deploy`.

- [ ] **Step 1: Write failing workflow contract tests**

```python
def test_workflow_passes_ci_head_sha_and_selected_services_to_safe_script(self):
    self.assertIn("github.event.workflow_run.head_sha", workflow)
    self.assertIn("classify-n100-safe-deployment.py", workflow)
    self.assertIn("deploy-n100-safe.sh", workflow)

def test_workflow_never_runs_safe_deploy_for_blocked_paths(self):
    self.assertIn("needs.changes.outputs.action == 'deploy'", workflow)
    self.assertIn("action=blocked", workflow)
```

- [ ] **Step 2: Run tests to verify RED**

Run: `PYTHONPATH=. python3 -m unittest tests.test_deploy_n100 tests.test_n100_safe_deployment`

Expected: FAIL because the workflow currently calls `deploy-n100.sh` after a broad runtime detector.

- [ ] **Step 3: Replace the broad detector with safe classification**

Use the CI `head_sha` checkout and call the classifier with `HEAD^` and `HEAD`. Make blocked runtime changes fail in the `changes` job before a self-hosted runner is allocated. Pass the exact SHA and space-separated selected services to WSL. Keep `contents: read`, main+successful CI gating, and non-cancelling concurrency. Do not add SSH keys, GitHub Secrets, K3s commands, or workflow dispatch.

- [ ] **Step 4: Run workflow contract tests to verify GREEN**

Run: `PYTHONPATH=. python3 -m unittest tests.test_deploy_n100 tests.test_n100_safe_deployment`

Expected: PASS and no references remain to broad `git reset --hard origin/main` deployment from the CD workflow.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/deploy-n100.yml tests/test_deploy_n100.py tests/test_n100_safe_deployment.py
git commit -m "ci: 안전 대상만 N100 자동 배포"
```

### Task 4: 운영 문서와 final verification

**Files:**
- Modify: `docs/n100-github-auto-deploy.md`
- Modify: `infra/k8s/README.md` only if it claims K3s changes are automatically deployed
- Modify: `tests/test_documentation_index.py` only if the document index requires registration

**Interfaces:**
- Documents the exact `main CI success → revision-pinned safe deploy → health → one rollback` behavior and excluded paths.

- [ ] **Step 1: Write failing documentation assertions**

```python
def test_n100_cd_guide_documents_revision_pinning_and_safe_scope(self):
    self.assertIn("workflow_run.head_sha", guide)
    self.assertIn("직전 정상 revision", guide)
    self.assertIn("Portal", guide)
    self.assertIn("자동 배포 제외", guide)
```

- [ ] **Step 2: Run test to verify RED**

Run: `PYTHONPATH=. python3 -m unittest tests.test_deploy_n100`

Expected: FAIL because the guide currently describes resetting to current `origin/main` and broad Compose rebuilds.

- [ ] **Step 3: Update the operator guide**

Document allowlisted services, exact-SHA deployment, initial deploy behavior, one rollback, how to read Actions failures, and the explicit exclusions. State that N100 backup credential enrollment remains a separate manual, masked one-time action.

- [ ] **Step 4: Run focused and CI-equivalent tests**

Run:

```bash
PYTHONPATH=. python3 -m unittest tests.test_deploy_n100 tests.test_n100_safe_deployment tests.test_documentation_index
PYTHONPATH=portal-web python3 -m unittest tests.test_file_access tests.test_portal_dashboard tests.test_portal_security tests.test_homeops tests.test_homeops_notifier
PYTHONPATH=system-agent python3 -m unittest tests.system_agent.test_metrics
PYTHONPATH=crawler-worker python3 -m unittest tests.crawler_worker.test_datetime_format tests.crawler_worker.test_investing_news_rss tests.crawler_worker.test_news_service tests.crawler_worker.test_news_routes tests.crawler_worker.test_rss_news
PYTHONPATH=homeops-executor python3 -m unittest tests.homeops_executor.test_docker_ops
PYTHONPATH=youtube-memo python3 -m unittest tests.youtube_memo.test_video_titles
PYTHONPATH=book-memo python3 -m unittest tests.book_memo.test_book_service
PYTHONPATH=car-care-worker python3 -m unittest discover -s tests/car_care_worker
```

Expected: all targeted tests pass. Run `bash -n scripts/deploy-n100-safe.sh scripts/verify-n100-safe-deployment-health.sh`, `python3 scripts/run_change_harness.py` with the final git-name-status-z input, and `git diff --check`.

- [ ] **Step 5: Commit**

```bash
git add docs/n100-github-auto-deploy.md infra/k8s/README.md tests/test_deploy_n100.py tests/test_documentation_index.py
git commit -m "docs: N100 안전 자동 배포 운영 기준 갱신"
```
