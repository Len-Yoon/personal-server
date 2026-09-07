# N100 제한형 운영 실행기 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** GitHub Actions가 N100 Windows self-hosted runner를 통해 고정된 운영 작업만 WSL에서 실행하고 안전한 결과를 반환하게 함.

**Architecture:** 수동 실행 workflow는 choice input 하나를 고정 runner에 전달함. `scripts/run-n100-operations.sh`는 allowlist에 없는 입력을 즉시 거부하고, 각 operation을 명시된 읽기 전용·고정 apply·기존 안전 배포 경로로만 분기함. Kubernetes Secret은 key 존재 여부만 확인하며 값은 읽거나 출력하지 않음.

**Tech Stack:** GitHub Actions YAML, Windows `wsl.exe`, Bash, Python `unittest`, K3s `kubectl`.

**Spec:** `docs/superpowers/specs/2026-09-07-n100-operations-runner-design.md`

## Global Constraints

- Workflow는 `workflow_dispatch`만 사용하고 operation choice 외 input을 받지 않음.
- 허용 operation은 `diagnose`, `deploy_safe_crawler`, `verify_news_observability`, `apply_news_observability`만 사용함.
- 서버 기동 방식, scheduler, Caddy, Cloudflare Tunnel 설정, Secret 생성·수정, PVC·운영 데이터 변경을 하지 않음.
- K3s 변경은 `apply_news_observability`의 고정 ServiceMonitor·PrometheusRule 두 파일만 `sudo -n k3s kubectl`로 적용함.
- Secret 값·환경변수 값·sudo·rclone 비밀번호를 출력·저장·전달하지 않음.
- `deploy_safe_crawler`는 기존 `scripts/deploy-n100-safe.sh`만 사용하며 revision 고정·health·rollback 동작을 유지함.

---

### Task 1: 고정 operation Bash 실행기와 계약 테스트

**Files:**
- Create: `scripts/run-n100-operations.sh`
- Create: `tests/test_n100_operations.py`

**Interfaces:**
- Consumes: operation 하나와 `N100_OPERATIONS_PROJECT_ROOT`, `N100_OPERATIONS_ACTIONS_ROOT`, `N100_OPERATIONS_DEPLOY_SHA` 환경변수.
- Produces: `n100_operation=<operation> status=PASS|FAIL` 및 비밀값 없는 단계 이름.

- [ ] **Step 1: 실패하는 allowlist·입력 거부 테스트 작성**

```python
result = self.run_operation("unknown")
self.assertNotEqual(result.returncode, 0)
self.assertNotIn("kubectl", result.stdout + result.stderr)

result = self.run_operation("diagnose", "extra")
self.assertNotEqual(result.returncode, 0)
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `python3 -m unittest tests.test_n100_operations.N100OperationsTests.test_unknown_operation_is_rejected -v`

Expected: FAIL because the runner script does not exist.

- [ ] **Step 3: 최소 실행기 구현**

```bash
case "$operation" in
  diagnose|deploy_safe_crawler|verify_news_observability|apply_news_observability) ;;
  *) printf '%s\n' 'n100_operation=invalid status=FAIL' >&2; exit 2 ;;
esac
```

`diagnose`는 `docker ps`, cloudflared process 존재, fixed loopback/public health, `sudo -n k3s kubectl`의 fixed get 명령만 실행함. `verify_news_observability`는 container 내부의 token non-empty 확인, Secret key 존재를 `/dev/null`로 확인, fixed ServiceMonitor·PrometheusRule get만 실행함. `apply_news_observability`는 고정된 두 manifest의 client dry-run 뒤 apply만 수행함. `deploy_safe_crawler`는 SHA 형식 검증 뒤 `scripts/deploy-n100-safe.sh "$sha" crawler-worker`만 실행함.

- [ ] **Step 4: operation별 허용 명령·비밀값 비노출 테스트 추가**

```python
result = self.run_operation("apply_news_observability")
self.assertEqual(result.returncode, 0, result.stderr)
self.assertIn("servicemonitor", self.read_log())
self.assertNotIn("super-secret", result.stdout + result.stderr)
```

Mock `docker`, `pgrep`, `curl`, `sudo`, `kubectl`, `git` executables로 명령 인자와 stdout을 기록함. 테스트는 고정 manifest 두 개만 apply되는지, `sudo -n k3s`가 사용되는지, Secret value가 output에 없는지 검증함.

- [ ] **Step 5: 테스트 통과 확인**

Run: `python3 -m unittest tests.test_n100_operations -v`

Expected: PASS.

- [ ] **Step 6: 커밋**

```bash
git add scripts/run-n100-operations.sh tests/test_n100_operations.py
git commit -m "feat: N100 제한형 운영 실행기 추가"
```

### Task 2: 수동 GitHub Actions workflow와 계약 테스트

**Files:**
- Create: `.github/workflows/n100-operations.yml`
- Modify: `tests/test_n100_operations.py`

**Interfaces:**
- Consumes: workflow_dispatch의 `operation` choice 및 GitHub Actions `github.sha`.
- Produces: self-hosted Windows runner에서 `wsl.exe -d Ubuntu-24.04`로 호출되는 고정 operation.

- [ ] **Step 1: 실패하는 workflow 계약 테스트 작성**

```python
workflow = (ROOT / ".github/workflows/n100-operations.yml").read_text()
self.assertIn("workflow_dispatch", workflow)
self.assertIn("type: choice", workflow)
self.assertNotIn("type: string", workflow)
self.assertIn("runs-on: [self-hosted, Windows, X64]", workflow)
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `python3 -m unittest tests.test_n100_operations.N100OperationsWorkflowTests.test_workflow_is_manual_and_allowlisted -v`

Expected: FAIL because the workflow file does not exist.

- [ ] **Step 3: 최소 workflow 구현**

```yaml
on:
  workflow_dispatch:
    inputs:
      operation:
        type: choice
        options: [diagnose, deploy_safe_crawler, verify_news_observability, apply_news_observability]
```

Job은 self-hosted Windows runner와 단일 concurrency group을 사용함. workflow checkout revision을 `N100_OPERATIONS_ACTIONS_ROOT`로 전달하고, N100 운영 checkout·Actions checkout이 존재하는지 확인한 뒤 `wsl.exe -d Ubuntu-24.04 -- bash -lc`로 script를 호출함. deploy operation에만 `github.sha`를 전달함.

- [ ] **Step 4: 권한·입력·명령 경계 테스트 추가**

```python
self.assertNotIn("pull_request", workflow)
self.assertNotIn("schedule:", workflow)
self.assertNotIn("${{ inputs.command }}", workflow)
self.assertIn("N100_OPERATIONS_ACTIONS_ROOT", workflow)
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `python3 -m unittest tests.test_n100_operations -v`

Expected: PASS.

- [ ] **Step 6: 커밋**

```bash
git add .github/workflows/n100-operations.yml tests/test_n100_operations.py
git commit -m "ci: N100 수동 운영 실행 경로 추가"
```

### Task 3: 운영 문서와 변경 범위 검증

**Files:**
- Modify: `docs/n100-github-auto-deploy.md`
- Modify: `docs/operations-reference.md`
- Modify: `tests/test_n100_operations.py`

**Interfaces:**
- Consumes: 고정 operation 목록 및 GitHub Actions 실행 결과.
- Produces: 운영자가 Actions UI에서 실행·판단·실패 대응할 수 있는 한국어 절차.

- [ ] **Step 1: 실패하는 문서 계약 테스트 작성**

```python
for operation in ALLOWED_OPERATIONS:
    self.assertIn(operation, documentation)
self.assertIn("Secret 값", documentation)
self.assertIn("출력하지 않음", documentation)
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `python3 -m unittest tests.test_n100_operations.N100OperationsDocumentationTests.test_documentation_covers_all_operations -v`

Expected: FAIL because the operations workflow is not documented.

- [ ] **Step 3: 최소 운영 문서 추가**

문서는 GitHub `Actions → N100 Operations → Run workflow`의 선택 절차, 각 operation의 변경 여부, 실패 시 로그 확인 방법, Secret/PVC/Caddy/서버 기동 설정이 제외된다는 경계를 한국어로 기록함. `diagnose`로 먼저 확인한 뒤 필요한 고정 operation만 실행하도록 안내함.

- [ ] **Step 4: 문서 계약과 회귀 테스트 통과 확인**

Run: `python3 -m unittest tests.test_n100_operations tests.test_n100_safe_deployment -v`

Expected: PASS.

- [ ] **Step 5: 변경 범위 harness 실행**

```bash
base=$(git merge-base origin/main HEAD)
git diff --name-status -z --find-renames "$base" HEAD > /tmp/n100-operations-changes.z
python3 scripts/run_change_harness.py --input /tmp/n100-operations-changes.z --input-format git-name-status-z --agent-context --check-result maintenance=success
```

Expected: `Work status: ready_for_review` 또는 required check에 맞춘 검증 보완 필요 상태.

- [ ] **Step 6: 커밋**

```bash
git add docs/n100-github-auto-deploy.md docs/operations-reference.md tests/test_n100_operations.py
git commit -m "docs: N100 운영 실행기 절차 추가"
```

## Plan Self-Review

- Spec coverage: 고정 operation, WSL runner 경로, Secret 비노출, 두 고정 manifest 적용, 기존 안전 배포 재사용, 실패 처리, 테스트·문서화를 Task 1~3에 각각 배정함.
- Placeholder scan: `TBD`, `TODO`, 추상적 오류 처리 문구 없음.
- Type consistency: workflow operation 값과 shell allowlist·test `ALLOWED_OPERATIONS` 값이 동일한 네 문자열을 사용함.
