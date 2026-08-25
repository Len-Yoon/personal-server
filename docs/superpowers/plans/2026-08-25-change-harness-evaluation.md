# Change Harness Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 변경 경로와 외부 검증 결과를 안전한 작업 상태·증적으로 정규화하고, 고정 사례로 정책 판단 회귀를 검증하는 로컬 하네스를 구축함.

**Architecture:** `scripts/run_change_harness.py`가 기존 `scripts/verify_change_scope.py`를 공개 CLI로 호출해 정책 원문을 보존함. 하네스는 `check_results`에서 실행 검증 목록을 단일하게 도출하고, 정책 증거와 결합해 상태·사람 확인 사유·압축 요약을 JSON으로 출력함. fixture 기반 평가는 실제 하네스 CLI를 실행해 중요한 계약 필드와 종료 코드를 검증함.

**Tech Stack:** Python 3.11 표준 라이브러리, `unittest`, JSON, 기존 `verify_change_scope.py` CLI

**Spec:** `docs/superpowers/specs/2026-08-25-harness-evaluation-loop-design.md`

## Global Constraints

- 서버 기동·배포·Compose·Caddy·뉴스 스케줄러는 수정하거나 실행하지 않음.
- 기존 `verify_change_scope.py`의 공개 인자, JSON 출력, 종료 코드 계약을 변경하지 않음.
- 하네스는 운영 명령을 실행하지 않고 호출자가 제공한 검증 결과만 정규화함.
- `input_error` → `blocked` → `verification_incomplete` → `verification_failed` → `ready_for_review` 우선순위를 유지함.
- 하네스와 평가는 로컬에서만 실행하며 CI workflow를 수정하지 않음.

---

### Task 1: 하네스 CLI 계약을 실패하는 테스트로 고정

**Files:**
- Create: `tests/test_change_harness.py`
- Create: `scripts/run_change_harness.py`

**Interfaces:**
- Consumes: 경로 파일을 받는 `--input`, 선택적 `--input-format`, `--check-result NAME=RESULT` 인자
- Produces: stdout JSON의 `schema_version`, `policy_evidence`, `check_results`, `work_status`, `human_review_required`, `human_review_reasons`, `summary` 및 종료 코드

- [ ] **Step 1: `ready_for_review` 실패 테스트 작성**

```python
code, evidence = run_harness(
    ["portal-web/app/main.py"], check_results=("portal=success",)
)
assert code == 0
assert evidence["work_status"] == "ready_for_review"
assert evidence["summary"]["required_checks"] == ["portal"]
```

이 테스트가 잡을 오류: 하네스가 정상 서비스 변경을 정책 검사기에 전달하지 못하거나 성공 상태를 만들지 못하는 오류.

- [ ] **Step 2: 실패 확인**

Run: `python3 -m unittest tests.test_change_harness.ChangeHarnessTests.test_successful_required_check_is_ready_for_review`

Expected: `scripts/run_change_harness.py`가 없어서 실패함.

- [ ] **Step 3: 최소 CLI 구현**

`subprocess.run()`으로 정책 검사기를 호출하고, `portal=success` 결과에서 `executed_checks=["portal"]`을 도출함. 정책 JSON을 `policy_evidence`로 보존하고 성공 상태 JSON을 출력함.

- [ ] **Step 4: 통과 확인**

Run: `python3 -m unittest tests.test_change_harness.ChangeHarnessTests.test_successful_required_check_is_ready_for_review`

Expected: PASS.

### Task 2: 안전 상태 전이와 입력 오류를 테스트 우선으로 구현

**Files:**
- Modify: `tests/test_change_harness.py`
- Modify: `scripts/run_change_harness.py`

**Interfaces:**
- Consumes: Task 1 CLI 및 `--check-result NAME=success|failure`
- Produces: `blocked`, `verification_incomplete`, `verification_failed`, `input_error` 상태 및 종료 코드 `2`, `1`

- [ ] **Step 1: 실패하는 상태 전이 테스트 작성**

```python
cases = [
    (["scripts/deploy-n100.sh"], (), "blocked", 2),
    (["unknown-area/config.toml"], (), "blocked", 2),
    (["portal-web/app/main.py"], (), "verification_incomplete", 2),
    (["portal-web/app/main.py"], ("portal=failure",), "verification_failed", 2),
]
```

또한 금지 경로와 실패 검증이 동시 입력되면 `blocked`가 우선이고 두 사유가 모두 보존되는지, 허용되지 않은 검증 이름은 `input_error`인지 확인함.

이 테스트가 잡을 오류: 위험 변경을 검토 가능으로 오판하거나, 실패·누락 사유를 버리거나, 임의 검증명을 허용하는 오류.

- [ ] **Step 2: 실패 확인**

Run: `python3 -m unittest tests.test_change_harness`

Expected: 새 상태가 구현되지 않아 적어도 하나가 실패함.

- [ ] **Step 3: 최소 상태 계산 구현**

정책 증거의 최소 키·타입을 확인하고, 필수 검증 기준으로 누락·실패를 계산함. 사유 배열은 차단, 누락, 실패 순서로 누적함. 정책 호출·입력 파싱 오류는 `policy_evidence: null`인 `input_error`로 처리함.

- [ ] **Step 4: 통과 확인**

Run: `python3 -m unittest tests.test_change_harness`

Expected: PASS.

### Task 3: 대표 사례 fixture와 CLI 평가를 추가

**Files:**
- Create: `tests/fixtures/change_harness/ready_service.json`
- Create: `tests/fixtures/change_harness/ready_documentation.json`
- Create: `tests/fixtures/change_harness/blocked_path.json`
- Create: `tests/fixtures/change_harness/unclassified_path.json`
- Create: `tests/fixtures/change_harness/incomplete_check.json`
- Create: `tests/fixtures/change_harness/failed_check.json`
- Create: `tests/test_change_harness_evals.py`

**Interfaces:**
- Consumes: Task 2 하네스 CLI와 fixture의 `paths`, `check_results`, `expected_status`, `expected_exit_code`
- Produces: 6개 사례의 실제 CLI 동작을 검증하는 `unittest` 평가 세트

- [ ] **Step 1: 실패하는 평가 테스트 및 fixture 작성**

각 fixture에 아래의 독립 기대값을 기입함.

```json
{
  "paths": ["portal-web/app/main.py"],
  "check_results": ["portal=success"],
  "expected_status": "ready_for_review",
  "expected_exit_code": 0
}
```

테스트는 하네스 CLI를 실제 실행하고, 상태·종료 코드·`human_review_required`와 `summary`의 중요 필드를 비교함.

이 테스트가 잡을 오류: 개별 사례가 정책과 다른 상태를 내거나, 안전하지 않은 상태를 성공으로 표시하는 오류.

- [ ] **Step 2: 실패 확인**

Run: `python3 -m unittest tests.test_change_harness_evals`

Expected: fixture 또는 평가 실행기가 없어 실패함.

- [ ] **Step 3: fixture 로딩 평가 구현**

`Path.glob("*.json")`을 파일명 순서로 순회하고, fixture의 경로를 임시 입력 파일에 기록한 뒤 CLI 결과를 JSON으로 파싱함. `expected_status`와 `expected_exit_code`을 독립 리터럴로 검증함.

- [ ] **Step 4: 통과 확인**

Run: `python3 -m unittest tests.test_change_harness_evals`

Expected: 6개 사례 모두 PASS.

### Task 4: 증적 운영 문서와 전체 회귀를 검증

**Files:**
- Modify: `docs/agent-loop-evidence.md`
- Modify: `docs/superpowers/plans/2026-08-25-change-harness-evaluation.md`

**Interfaces:**
- Consumes: 하네스 증적 JSON과 6개 fixture 평가 결과
- Produces: 로컬 하네스 증적의 해석·제한·토큰 크기 측정 기준 문서화

- [x] **Step 1: 문서 검토 항목 작성**

문서에 하네스가 운영 명령을 실행하지 않는다는 경계, `input_error`·`blocked`·`verification_incomplete`·`verification_failed`·`ready_for_review` 상태별 처리, UTF-8 바이트 측정 산식과 25% 수용 목표, CI artifact 연계가 범위 밖이라는 사실을 추가함. 실제 원본 입력·출력 바이트와 모델 토큰은 측정하지 않아 확인 필요로 남김.

- [x] **Step 2: 전체 회귀 실행**

Run: `python3 -m unittest tests.test_verify_change_scope tests.test_change_harness tests.test_change_harness_evals`

Result: PASS (`Ran 27 tests ... OK`, exit 0).

- [ ] **Step 3: 변경 범위·공백 검증**

Run: `python3 scripts/verify_change_scope.py --input <changed-paths-file> --executed-checks maintenance && git diff --check`

Result: `git diff --check`는 PASS(exit 0)했으나, 현재 정책에서 `scripts/run_change_harness.py`가 `blocked_files`로 분류되어 scope 검증은 exit 2임. 문서·테스트·하네스 전체가 `maintenance`로 분류된다는 기대는 충족되지 않아 확인 필요함. 입력 경로는 tracked/untracked 변경 파일에서 `.superpowers/` 경로를 제외해 구성함.

- [x] **Step 4: 계획 완료 표기**

검증 명령과 실제 결과를 각 체크박스에 반영하고, 미측정 토큰 원본 및 scope 분류 불일치는 확인 필요 사항으로 남김.

### Task 5: 하네스 파일의 maintenance 정책 분류를 최소 범위로 허용

**Files:**
- Modify: `scripts/verify_change_scope.py`
- Modify: `tests/test_verify_change_scope.py`

- [ ] **Step 1: 실패하는 정책 회귀 테스트 작성**

```python
code, evidence = run_scope(
    "scripts/run_change_harness.py", executed_checks=("maintenance",)
)
assert code == 0
assert evidence["automation_files"] == ["scripts/run_change_harness.py"]
assert evidence["required_checks"] == ["maintenance"]
assert evidence["blocked_files"] == []
```

이 테스트가 잡을 오류: 하네스만 허용하려는 정책 예외가 전체 `scripts/**` 금지 규칙을 완화하거나, 새 하네스를 계속 차단하는 오류.

- [ ] **Step 2: 실패 확인**

Run: `python3 -m unittest tests.test_verify_change_scope.VerifyChangeScopeTests.test_change_harness_is_maintenance_policy_file`

Expected: 현재 `scripts/**` 차단 때문에 실패함.

- [ ] **Step 3: 최소 정책 변경**

`POLICY_MAINTENANCE_FILES`에 `scripts/run_change_harness.py`만 추가함. 이 목록의 판정은 `BLOCKED_PREFIXES`보다 먼저 유지해 다른 `scripts/**` 경로는 계속 차단됨.

- [ ] **Step 4: 통과 및 전체 범위 확인**

Run: `python3 -m unittest tests.test_verify_change_scope tests.test_change_harness tests.test_change_harness_evals`

Expected: PASS. `scripts/deploy-n100.sh`, `scripts/maintenance.py`는 기존처럼 차단됨.

### Task 6: 하네스 증적을 에이전트 작업 컨텍스트로 연결

**Files:**
- Modify: `scripts/run_change_harness.py`
- Modify: `tests/test_change_harness.py`
- Modify: `AGENTS.md`
- Modify: `docs/agent-loop-evidence.md`

- [ ] **Step 1: 실패하는 agent-context 출력 테스트 작성**

정상·차단·검증 누락 사례에서 CLI `--agent-context`가 JSON 전체가 아닌 상태, 필수·누락 검증, 차단 파일, 다음 조치만 포함한 짧은 Markdown을 출력하는지 검증함.

- [ ] **Step 2: 실패 확인**

Run: `python3 -m unittest tests.test_change_harness.ChangeHarnessTests.test_cli_agent_context_compacts_incomplete_work`

Expected: 옵션 미지원으로 실패함.

- [ ] **Step 3: 최소 구현 및 작업 규칙 연결**

`--agent-context` 옵션은 기존 증적을 재계산하지 않고 이미 생성한 증적을 텍스트로 변환함. `AGENTS.md`에는 코드·설정 변경 전 하네스 요약을 확인하고, 검증 후 실행 결과를 넣어 다시 확인하도록 명시함. 문서에는 자동 Codex 주입이 아니라 저장소 작업 규칙을 통한 재사용임을 명시함.

- [ ] **Step 4: 통과 확인**

Run: `python3 -m unittest tests.test_change_harness tests.test_change_harness_evals tests.test_verify_change_scope`

Expected: PASS.
