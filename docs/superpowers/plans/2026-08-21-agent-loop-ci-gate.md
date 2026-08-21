# 에이전트 작업 루프 CI 게이트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 변경 범위에 맞는 검증 증거를 남기고 금지·미분류 변경을 중단하는 CI 기반 에이전트 작업 완료 루프를 구현함.

**Architecture:** 순수 Python 검사기 `scripts/verify_change_scope.py`가 변경 파일 목록을 서비스·문서·인프라·금지·미분류 범주로 판정하고 JSON evidence를 생성함. 기존 CI는 검사기 결과와 전체 서비스 matrix 테스트 결과를 집계해 artifact와 summary로 남기며, PR 전용 workflow는 동일 검사기로 금지·미분류 변경을 독립 차단함.

**Tech Stack:** Python 3.11 표준 라이브러리, `unittest`, GitHub Actions, Markdown

**Spec:** `docs/superpowers/specs/2026-08-21-agent-loop-ci-gate-design.md`

## Global Constraints

- 적용 범위는 개인서버 저장소에 한정함.
- `scripts/deploy-n100.sh`, `crawler-worker/app/services/news_scheduler.py`, Compose 서비스 기동 동작은 변경하지 않음.
- GitHub Actions에서 Codex 또는 외부 LLM을 호출해 자동 수정·리뷰 댓글·재푸시하지 않음.
- 자동 병합, 자동 배포, 자동 롤백, 외부 알림 연동은 포함하지 않음.
- 분류되지 않은 변경과 금지 영역 변경은 검토 실패로 처리함.
- 검증 불가 항목은 성공으로 처리하지 않고 `확인 필요`로 기록함.

---

### Task 1: 변경 범위 검사기와 단위 테스트 구현

**Files:**
- Create: `scripts/verify_change_scope.py`
- Create: `tests/test_verify_change_scope.py`

**Interfaces:**
- Consumes: `--input <변경 파일 목록 텍스트 파일>`; 한 줄에 저장소 루트 기준 파일 경로 하나. 선택 인자 `--test-result <GitHub job 결과>`를 지원함.
- Produces: 표준 출력 JSON 객체. 키는 `changed_files`, `services`, `documentation_files`, `infrastructure_files`, `blocked_files`, `unclassified_files`, `required_checks`이며 `--test-result` 제공 시 `test_result`를 포함함.
- Exit codes: `0`은 정책상 검토 가능, `2`는 `blocked_files` 또는 `unclassified_files` 존재, `1`은 입력·실행 오류임.

- [ ] **Step 1: 실패하는 서비스 분류 테스트 작성**

`tests/test_verify_change_scope.py`에 아래 테스트와 임시 입력 파일 생성 helper를 작성함.

```python
def run_scope(*paths: str) -> tuple[int, dict]:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write("\n".join(paths))
        input_path = Path(handle.name)
    completed = subprocess.run(
        [sys.executable, "scripts/verify_change_scope.py", "--input", str(input_path)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    input_path.unlink()
    return completed.returncode, json.loads(completed.stdout)

def test_portal_change_requires_portal_check():
    code, evidence = run_scope("portal-web/app/main.py")
    assert code == 0
    assert evidence["services"] == ["portal"]
    assert evidence["required_checks"] == ["portal"]
```

같은 파일에 `system-agent`, `crawler-worker`, `homeops-executor`, `youtube-memo`, `book-memo` 경로가 각각 하나의 동일 이름 서비스와 `required_checks`로 분류되는 parameterized subTest를 추가함.

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `python3 -m unittest tests.test_verify_change_scope -v`

Expected: FAIL. `scripts/verify_change_scope.py`가 없어서 subprocess 실행 또는 JSON 파싱이 실패함.

- [ ] **Step 3: 문서·인프라·중단 정책 테스트 추가**

같은 테스트 파일에 아래 정책을 추가함.

```python
def test_documentation_change_needs_no_service_test():
    code, evidence = run_scope("README.md", "docs/codex-work-loop.md")
    assert code == 0
    assert evidence["documentation_files"] == ["README.md", "docs/codex-work-loop.md"]
    assert evidence["required_checks"] == []

def test_scheduler_change_blocks_review():
    code, evidence = run_scope("crawler-worker/app/services/news_scheduler.py")
    assert code == 2
    assert evidence["blocked_files"] == ["crawler-worker/app/services/news_scheduler.py"]

def test_unknown_change_blocks_review():
    code, evidence = run_scope("unknown-area/config.toml")
    assert code == 2
    assert evidence["unclassified_files"] == ["unknown-area/config.toml"]
```

`docker-compose.yml`, `docker-compose.n100.yml`, `scripts/maintenance.py`, `caddy/Caddyfile`은 `infrastructure_files`에 기록되고 exit code `0`을 반환하는 테스트도 추가함. `scripts/deploy-n100.sh`는 `blocked_files`에 기록되고 exit code `2`를 반환하는 테스트를 추가함.

- [ ] **Step 4: 최소 검사기 구현**

`scripts/verify_change_scope.py`에 다음 상수를 정의함.

```python
SERVICE_PREFIXES = {
    "portal-web/": "portal",
    "system-agent/": "system-agent",
    "crawler-worker/": "crawler-worker",
    "homeops-executor/": "homeops-executor",
    "youtube-memo/": "youtube-memo",
    "book-memo/": "book-memo",
}
DOCUMENTATION_PREFIXES = ("docs/",)
DOCUMENTATION_FILES = {"README.md", "AGENTS.md", "CLAUDE.md"}
INFRASTRUCTURE_PREFIXES = ("caddy/",)
INFRASTRUCTURE_FILES = {
    "docker-compose.yml",
    "docker-compose.n100.yml",
}
INFRASTRUCTURE_SCRIPT_PREFIX = "scripts/"
BLOCKED_FILES = {
    "scripts/deploy-n100.sh",
    "crawler-worker/app/services/news_scheduler.py",
}
```

`classify_paths(paths: list[str]) -> dict[str, list[str]]`는 중복을 제거하되 입력 순서를 유지함. `main()`은 `--input` 파일의 공백 행을 제외한 경로를 읽고, 제공된 `--test-result`를 JSON에 추가한 뒤 출력하며, `blocked_files` 또는 `unclassified_files`가 있으면 `2`를 반환함. JSON 키와 리스트 순서는 테스트 가능한 고정 순서로 유지함.

- [ ] **Step 5: 검사기 단위 테스트 실행**

Run: `python3 -m unittest tests.test_verify_change_scope -v`

Expected: PASS. 서비스, 문서, 인프라, 금지, 미분류 정책이 모두 검증됨.

- [ ] **Step 6: CLI 수동 검증 실행**

Run: `printf 'portal-web/app/main.py\nREADME.md\n' > /tmp/agent-loop-files.txt && python3 scripts/verify_change_scope.py --input /tmp/agent-loop-files.txt`

Expected: `services`와 `required_checks`에는 `portal`, `documentation_files`에는 `README.md`가 포함된 JSON을 출력하고 exit code `0`으로 종료함.

### Task 2: CI evidence 집계 게이트 추가

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_compose_config.py`
- Modify: `tests/test_maintenance.py`

**Interfaces:**
- Consumes: Task 1의 `scripts/verify_change_scope.py` JSON 표준 출력
- Produces: workflow summary, `agent-loop-scope.json` artifact, 기존 서비스 matrix 테스트 결과

- [ ] **Step 1: CI workflow 구조 테스트 작성**

기존 workflow 구조를 검사하는 테스트 파일 중 프로젝트 관례에 맞는 하나에 아래 assertions를 추가함.

```python
workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
assert "scope:" in workflow
assert "verify_change_scope.py" in workflow
assert "agent-loop-scope.json" in workflow
assert "agent-loop-evidence" in workflow
assert "summary:" in workflow
```

테스트는 기존 service matrix 이름과 test command가 유지되는지도 함께 assertion함.

- [ ] **Step 2: 구조 테스트가 실패하는지 확인**

Run: `python3 -m unittest tests.test_compose_config tests.test_maintenance -v`

Expected: FAIL. `ci.yml`에 scope·summary·artifact 구성이 아직 없음.

- [ ] **Step 3: CI scope job 추가**

`.github/workflows/ci.yml`에 `scope` job을 test job보다 앞에 추가함.

```yaml
  scope:
    runs-on: ubuntu-latest
    outputs:
      policy_status: ${{ steps.scope.outputs.policy_status }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Collect changed files
        id: files
        shell: bash
        run: |
          if [ "${{ github.event_name }}" = "pull_request" ]; then
            git diff --name-only "${{ github.event.pull_request.base.sha }}" "${{ github.sha }}" > changed-files.txt
          else
            git diff --name-only "${{ github.event.before }}" "${{ github.sha }}" > changed-files.txt
          fi
      - name: Classify change scope
        id: scope
        shell: bash
        run: |
          set +e
          python3 scripts/verify_change_scope.py --input changed-files.txt > agent-loop-scope.json
          status=$?
          echo "policy_status=$status" >> "$GITHUB_OUTPUT"
          exit 0
```

첫 push에서 `github.event.before`가 40개의 `0`인 경우에는 `git diff-tree --no-commit-id --name-only -r "${{ github.sha }}"`를 사용하도록 조건을 추가함. JSON과 `changed-files.txt`를 `actions/upload-artifact@v4`로 `agent-loop-evidence` 이름에 업로드함.

- [ ] **Step 4: 기존 테스트 job과 summary job 연결**

`test` job에 `needs: scope`를 추가하되, scope의 정책 상태와 관계없이 테스트 evidence를 남기도록 `if: always()`를 사용함. matrix와 요구사항 설치·test command는 그대로 유지함.

`summary` job을 추가하고 `needs: [scope, test]`, `if: always()`로 설정함. summary job은 아래를 수행함.

```yaml
      - name: Write agent-loop summary
        shell: bash
        run: |
          echo "## Agent loop verification" >> "$GITHUB_STEP_SUMMARY"
          echo "- Scope policy status: ${{ needs.scope.outputs.policy_status }}" >> "$GITHUB_STEP_SUMMARY"
          echo "- Service test result: ${{ needs.test.result }}" >> "$GITHUB_STEP_SUMMARY"
          cat agent-loop-scope.json >> "$GITHUB_STEP_SUMMARY"
      - name: Enforce loop gate
        shell: bash
        run: |
          test "${{ needs.scope.outputs.policy_status }}" = "0"
          test "${{ needs.test.result }}" = "success"
```

summary job은 scope artifact에서 `changed-files.txt`를 다운로드한 뒤 `python3 scripts/verify_change_scope.py --input changed-files.txt --test-result "${{ needs.test.result }}" > agent-loop-evidence.json`을 실행함. 결과 JSON을 `agent-loop-evidence` artifact로 업로드함.

- [ ] **Step 5: 구조 테스트 재실행**

Run: `python3 -m unittest tests.test_compose_config tests.test_maintenance -v`

Expected: PASS. 기존 CI matrix와 새 scope·summary·artifact 구성이 모두 확인됨.

- [ ] **Step 6: YAML·변경 범위 검토**

Run: `git diff --check && git diff -- .github/workflows/ci.yml tests/test_compose_config.py tests/test_maintenance.py`

Expected: 공백 오류가 없고, 배포 workflow·서버 기동·스케줄러 파일이 변경되지 않음.

### Task 3: PR 독립 정책 검토 workflow 구현

**Files:**
- Create: `.github/workflows/agent-review.yml`
- Modify: `tests/test_compose_config.py`

**Interfaces:**
- Consumes: PR base SHA와 head SHA, Task 1의 검사기
- Produces: `agent-review-scope.json` artifact 및 PR 정책 성공·실패 상태

- [ ] **Step 1: PR 검토 workflow 구조 테스트 추가**

`tests/test_compose_config.py`에 다음 검증을 추가함.

```python
workflow = Path(".github/workflows/agent-review.yml").read_text(encoding="utf-8")
assert "pull_request:" in workflow
assert "fetch-depth: 0" in workflow
assert "verify_change_scope.py" in workflow
assert "agent-review-scope" in workflow
assert "policy_status" in workflow
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `python3 -m unittest tests.test_compose_config -v`

Expected: FAIL. PR 검토 workflow 파일이 아직 없음.

- [ ] **Step 3: 독립 정책 검토 workflow 작성**

`.github/workflows/agent-review.yml`을 아래 구조로 생성함.

```yaml
name: Agent Review

on:
  pull_request:

permissions:
  contents: read

jobs:
  policy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Collect pull request changes
        run: git diff --name-only "${{ github.event.pull_request.base.sha }}" "${{ github.event.pull_request.head.sha }}" > changed-files.txt
      - name: Review change policy
        id: scope
        shell: bash
        run: |
          set +e
          python3 scripts/verify_change_scope.py --input changed-files.txt > agent-review-scope.json
          status=$?
          echo "policy_status=$status" >> "$GITHUB_OUTPUT"
          exit 0
      - uses: actions/upload-artifact@v4
        with:
          name: agent-review-scope
          path: agent-review-scope.json
      - name: Enforce review gate
        run: test "${{ steps.scope.outputs.policy_status }}" = "0"
```

workflow는 PR 댓글, 외부 API 호출, 자동 수정 권한을 추가하지 않음.

- [ ] **Step 4: 구조 테스트 재실행**

Run: `python3 -m unittest tests.test_compose_config -v`

Expected: PASS. PR trigger, scope 검사, artifact, 정책 게이트가 확인됨.

### Task 4: 증거 운영 문서와 README 안내 반영

**Files:**
- Create: `docs/agent-loop-evidence.md`
- Modify: `README.md`
- Modify: `tests/test_verify_change_scope.py`

**Interfaces:**
- Consumes: CI `agent-loop-evidence` 및 PR `agent-review-scope` artifact
- Produces: artifact 해석·개선 이력 기준과 사실 기반 README 안내

- [ ] **Step 1: 증거 문서 구조 작성**

`docs/agent-loop-evidence.md`에 다음 섹션을 작성함.

```markdown
# 에이전트 작업 루프 검증 증거

## 1. 목적
## 2. CI artifact 확인 방법
## 3. 결과 분류 기준
## 4. 개선 이력 기록 양식
## 5. 확인 필요 사항
```

결과 분류 표에는 `성공`, `정책 중단`, `테스트 실패`, `검증 불가`, `확인 필요`를 포함함. 개선 이력 양식에는 일시, 변경 범위, 실패 유형, 재시도 횟수, 조치, 잔여 위험을 포함함.

- [ ] **Step 2: README 사실 기반 안내 추가**

`README.md`의 Codex 사용 안내를 아래 의미로 갱신함.

```markdown
이 저장소에서는 Codex 작업 완료 루프를 문서화하고, 변경 범위 판정·서비스별 CI 검증·PR 정책 검토 결과를 artifact로 기록합니다. 자동 코드 수정·자동 병합·무인 배포는 수행하지 않으며, 금지·미분류 변경과 검증 실패는 중단 후 확인 대상으로 처리합니다.
```

“완전 자율”, “모든 변경 자동 검증”, “잘 사용하고 있음” 같은 평가·과장 표현은 추가하지 않음.

- [ ] **Step 3: 문서 경로 분류 테스트 추가**

`tests/test_verify_change_scope.py`의 문서 변경 테스트에 `docs/agent-loop-evidence.md`를 추가해 코드 테스트가 요구되지 않는지 검증함.

- [ ] **Step 4: 문서·검사기 테스트 실행**

Run: `python3 -m unittest tests.test_verify_change_scope tests.test_compose_config tests.test_maintenance -v`

Expected: PASS. 범위 정책과 workflow 구조가 통과함.

- [ ] **Step 5: 최종 변경 범위 검토**

Run: `git diff --check && git status --short && git diff -- AGENTS.md README.md docs scripts/verify_change_scope.py tests/test_verify_change_scope.py .github/workflows/ci.yml .github/workflows/agent-review.yml`

Expected: 공백 오류가 없고 서버 기동·스케줄러·배포 workflow 변경이 없음.

- [ ] **Step 6: 커밋**

Run: `git add AGENTS.md README.md docs scripts/verify_change_scope.py tests/test_verify_change_scope.py tests/test_compose_config.py tests/test_maintenance.py .github/workflows/ci.yml .github/workflows/agent-review.yml && git commit -m "feat: 에이전트 작업 루프 CI 게이트 추가"`

Expected: CI 기반 검증·검토·증거 문서가 한글 커밋 메시지로 기록됨. Git 권한 또는 기존 사용자 변경사항으로 커밋이 불가하면 커밋하지 않고 사유를 보고함.
