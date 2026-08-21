# 문서 수명주기 정리 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 현재 운영 문서를 단일 진입점으로 정리하고, 과거 설계·계획 이력은 보존하면서 Markdown 예시를 실제 문서와 혼동하지 않도록 검증함.

**Architecture:** `docs/README.md`가 현재 운영 문서, 참고 자료, 과거 이력을 구분하는 색인 역할을 담당함. 운영 문서는 서로 기준 문서를 연결하고, `superpowers`는 완료된 작업의 설계·계획 이력만 보관함. 문서 링크와 plan 최상위 제목 수를 자동 검증함.

**Tech Stack:** Markdown, Python 3.11 `unittest`

**Spec:** `docs/superpowers/specs/2026-08-22-documentation-lifecycle-audit-design.md`

## Global Constraints

- 서버 기동, 스케줄러, Compose, Dockerfile, workflow, 서비스 코드를 변경하지 않음.
- 과거 QA 보고서와 `superpowers`의 개별 설계·계획 파일을 삭제하지 않음.
- 현재 운영 지침은 설정·workflow·테스트로 확인 가능한 사실만 기록함.
- 상대 Markdown 링크, 문서 정책 테스트, `git diff --check`를 통과해야 함.

---

### Task 1: 문서 색인과 현재 운영 문서 기준 정리

**Files:**
- Create: `tests/test_documentation_index.py`
- Modify: `docs/README.md`
- Modify: `docs/agent-handoff.md`
- Modify: `docs/operations-reference.md`
- Modify: `docs/cloudflare-tunnel.md`
- Modify: `docs/caddy-cloudflare.md`
- Modify: `docs/codex-work-loop.md`
- Modify: `docs/agent-loop-evidence.md`
- Modify: `docs/superpowers/README.md`

**Interfaces:**
- Consumes: 현재 docs 파일 경로와 `docs/codex-work-loop.md`의 브랜치 정리 규칙.
- Produces: `docs/README.md`의 `현재 운영 기준`, `참고 자료`, `과거 이력` 분류 및 검증 가능한 상대 링크.

- [ ] **Step 1: 문서 색인 계약 테스트를 작성함**

`tests/test_documentation_index.py`에 아래 테스트를 추가함.

```python
from pathlib import Path
import unittest


class DocumentationIndexTests(unittest.TestCase):
    def test_document_index_separates_current_operations_and_history(self):
        content = Path("docs/README.md").read_text(encoding="utf-8")

        self.assertIn("## 현재 운영 기준", content)
        self.assertIn("## 참고 자료", content)
        self.assertIn("## 과거 이력", content)
        self.assertIn("codex-work-loop.md", content)
        self.assertIn("agent-loop-evidence.md", content)
```

- [ ] **Step 2: 테스트가 현재 색인 제목 부재로 실패하는지 확인함**

Run: `python3 -m unittest tests.test_documentation_index -v`

Expected: `현재 운영 기준` 제목 부재로 실패함.

- [ ] **Step 3: 현재 문서의 기준·연결 관계를 갱신함**

다음 내용을 반영함.

- `docs/README.md`의 분류와 권장 열람 순서를 갱신하고 2026-08-22 점검 기준을 기록함.
- `agent-handoff.md`의 변경 흐름을 `codex-work-loop.md` 기준으로 연결하고, PR은 변경 위험도에 따라 선택하되 병합 뒤 정리 조건을 따르도록 정리함.
- `operations-reference.md`, Tunnel, Caddy 문서에는 공통 경로·포트의 기준 문서가 운영 참조임을 명시하고, Tunnel/Caddy 중 하나만 공개 경로로 선택한다는 원칙을 유지함.
- `codex-work-loop.md`와 `agent-loop-evidence.md`에 서로의 목적을 연결하고 artifact 90일 보존 한계를 유지함.
- `superpowers/README.md`에 설계·계획 파일은 현재 실행 지침이 아닌 이력이라는 사용 기준을 명시함.

- [ ] **Step 4: 문서 색인 계약 테스트가 통과하는지 확인함**

Run: `python3 -m unittest tests.test_documentation_index -v`

Expected: PASS.

- [ ] **Step 5: 작업 파일을 명시적으로 stage하고 커밋함**

Run: `git add tests/test_documentation_index.py docs/README.md docs/agent-handoff.md docs/operations-reference.md docs/cloudflare-tunnel.md docs/caddy-cloudflare.md docs/codex-work-loop.md docs/agent-loop-evidence.md docs/superpowers/README.md && git commit -m "docs: 현재 운영 문서 색인 정리"`

### Task 2: 계획 이력의 Markdown 예시 검증

**Files:**
- Modify: `tests/test_documentation_index.py`

**Interfaces:**
- Consumes: fenced Markdown 예시를 포함한 두 이력 plan.
- Produces: fenced block을 제외한 실제 최상위 제목 수를 검증하는 테스트.

- [ ] **Step 1: 중복 본문 방지 테스트를 작성함**

`tests/test_documentation_index.py`에 아래 테스트를 추가함.

```python
    def test_history_plans_keep_examples_inside_fenced_blocks(self):
        plan_paths = [
            "docs/superpowers/plans/2026-08-21-agent-loop-ci-gate.md",
            "docs/superpowers/plans/2026-08-21-codex-work-completion-loop.md",
        ]

        for path in plan_paths:
            in_fence = False
            headings = []
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                if line.startswith("```"):
                    in_fence = not in_fence
                    continue
                if not in_fence and line.startswith("# "):
                    headings.append(line)
            self.assertEqual(len(headings), 1, path)
```

- [ ] **Step 2: 단순 제목 수 검사가 fenced 예시를 오인하는지 확인함**

Run: `python3 -m unittest tests.test_documentation_index.DocumentationIndexTests.test_history_plans_do_not_embed_current_document_copies -v`

Expected: fenced code block 내부 제목까지 세어 실패함.

- [ ] **Step 3: fenced code block을 제외하도록 제목 검사 방식을 수정함**

코드 펜스 시작·종료를 추적하고 fence 밖의 `# ` 제목만 세도록 테스트를 수정함. plan 내용과 별도 현재 문서는 변경하지 않음.

- [ ] **Step 4: 이력 파일 계약 테스트를 실행함**

Run: `python3 -m unittest tests.test_documentation_index -v`

Expected: PASS.

- [ ] **Step 5: 작업 파일을 명시적으로 stage하고 커밋함**

Run: `git add tests/test_documentation_index.py docs/superpowers/specs/2026-08-22-documentation-lifecycle-audit-design.md docs/superpowers/plans/2026-08-22-documentation-lifecycle-audit.md && git commit -m "test: 문서 이력 Markdown 예시 검증"`

### Task 3: 전체 문서 검증과 완료 보고

**Files:**
- Verify only: `docs/**/*.md`, `tests/test_documentation_index.py`

**Interfaces:**
- Consumes: Task 1·2의 문서 분류와 중복 제거 결과.
- Produces: 문서 링크·정책·공백 오류 검증 결과.

- [ ] **Step 1: 전체 문서 링크를 검사함**

Run:

```bash
python3 - <<'PY'
import re
from pathlib import Path

for document in Path("docs").rglob("*.md"):
    content = document.read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)#]+)", content):
        if "://" not in target and not (document.parent / target).exists():
            raise SystemExit(f"broken link: {document} -> {target}")
PY
```

Expected: 종료 코드 0. 외부 URL은 검사 대상에서 제외함.

- [ ] **Step 2: 문서·정책 회귀 테스트를 실행함**

Run: `python3 -m unittest tests.test_documentation_index tests.test_verify_change_scope tests.test_compose_config -v`

Expected: PASS.

- [ ] **Step 3: 변경 범위와 공백 오류를 확인함**

Run: `git diff --check && git diff --name-only origin/main...HEAD`

Expected: `docs/`, `tests/test_documentation_index.py` 외 서비스·운영 파일 변경 없음.

- [ ] **Step 4: 최종 커밋과 PR 준비 상태를 확인함**

Run: `git status --short --branch && git log --oneline origin/main..HEAD`

Expected: 문서 정리 커밋만 존재하고 작업공간 변경이 없음.
