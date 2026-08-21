# Codex 작업 완료 루프 반영 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 개인서버 저장소에서 Codex가 코드 작업 시 검증·재시도·중단·보고 절차를 일관되게 따르도록 프로젝트 규칙을 반영함.

**Architecture:** `AGENTS.md`에는 모든 작업에서 즉시 확인해야 하는 필수 규칙과 상세 문서 링크만 추가함. `docs/codex-work-loop.md`에는 단계별 작업 절차, 검증·중단 기준, 완료 보고 양식을 분리하여 관리함. 실행 자동화와 전역 Codex 설정은 추가하지 않음.

**Tech Stack:** Markdown, Git, 기존 Python·JavaScript 테스트 구성

**Spec:** `docs/superpowers/specs/2026-08-21-codex-work-completion-loop-design.md`

## Global Constraints

- 적용 범위는 개인서버 저장소에 한정함.
- `서버 띄우는 쪽`과 `스케줄러 쪽`은 변경하지 않음.
- Codex 전역 설정, 전역 스킬, 자동화 일정, CI/CD workflow는 변경하지 않음.
- 검증 불가 항목은 성공으로 처리하지 않고 `확인 필요`로 보고함.
- 재검증은 동일 작업에서 최대 3회까지만 수행함.

---

### Task 1: 필수 작업 완료 루프 규칙 추가

**Files:**
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: `docs/codex-work-loop.md`의 상세 절차
- Produces: 모든 Codex 작업이 확인하는 요약 규칙과 상세 문서 경로

- [ ] **Step 1: 기존 프로젝트 제한 규칙 확인**

Run: `sed -n '1,160p' AGENTS.md`

Expected: 서버 기동·스케줄러 변경 금지와 한글 커밋 메시지 규칙이 확인됨.

- [ ] **Step 2: 작업 완료 루프 요약 규칙 추가**

`AGENTS.md`에 `## Codex 작업 완료 루프` 섹션을 추가하고 아래 내용을 반영함.

```markdown
- 코드·설정 변경 전 변경 범위, 제외 범위, 성공 기준을 확인함.
- 관련 테스트와 적용 가능한 정적 검사를 실행하며, 통과 전에는 완료로 보고하지 않음.
- 실패 원인을 확인한 최소 수정만 수행하고, 동일 작업의 재검증은 최대 3회로 제한함.
- 범위 불명확, 금지 영역 접근, 보안·운영·배포 영향 판단 필요 시 작업을 중단하고 사용자 확인을 요청함.
- 완료 보고에는 변경 내용, 변경 파일, 검증 결과, 미검증 항목, 확인 필요 사항을 포함함.
- 상세 절차는 `docs/codex-work-loop.md`를 따름.
```

- [ ] **Step 3: 규칙 문서 구조 검증**

Run: `rg -n "Codex 작업 완료 루프|codex-work-loop\.md|서버 띄우는 쪽|스케줄러" AGENTS.md`

Expected: 기존 제한 규칙과 새 작업 완료 루프 규칙이 모두 검색됨.

- [ ] **Step 4: 변경사항 검토**

Run: `git diff --check && git diff -- AGENTS.md`

Expected: 공백 오류가 없고, 새 규칙이 기존 범위 제한을 약화하지 않음.

### Task 2: 상세 작업 완료 루프 문서 작성

**Files:**
- Create: `docs/codex-work-loop.md`

**Interfaces:**
- Consumes: `AGENTS.md`의 상세 절차 참조
- Produces: 작업 단계, 검증·중단 기준, 완료 보고 양식

- [ ] **Step 1: 상세 문서 작성**

`docs/codex-work-loop.md`에 아래 목차와 내용을 작성함.

```markdown
# Codex 작업 완료 루프

## 1. 적용 범위
## 2. 작업 절차
## 3. 검증 기준
## 4. 실패 재시도 및 중단 기준
## 5. 완료 보고 양식
## 6. 제외 범위
```

작업 절차에는 `요청 확인 → 범위·성공 기준 확인 → 계획·승인 → 최소 변경 → 검증 → 재시도 또는 보고` 순서를 명시함. 검증 불가 시 `확인 필요`로 보고하는 규칙, 최대 3회 재검증 규칙, 위험 변경 전 사용자 확인 규칙을 각각 표로 작성함.

- [ ] **Step 2: 완료 보고 양식 작성**

문서에 아래 표를 포함함.

```markdown
| 항목 | 내용 |
|---|---|
| 변경 내용 |  |
| 변경 파일 |  |
| 검증 결과 |  |
| 미검증 항목 |  |
| 확인 필요 사항 |  |
```

- [ ] **Step 3: 문서 내용 검증**

Run: `rg -n "최대 3회|확인 필요|사용자 확인|변경 내용|변경 파일|검증 결과" docs/codex-work-loop.md`

Expected: 재시도 한도, 중단 조건, 완료 보고 필수 항목이 모두 검색됨.

- [ ] **Step 4: Markdown 및 변경 범위 검토**

Run: `git diff --check && git diff -- AGENTS.md docs/codex-work-loop.md`

Expected: 공백 오류가 없고, 계획에 명시한 두 파일 외의 운영 코드·설정 변경이 없음.

### Task 3: 최종 문서 일관성 확인 및 커밋

**Files:**
- Modify: `AGENTS.md`
- Create: `docs/codex-work-loop.md`

**Interfaces:**
- Consumes: Task 1의 요약 규칙과 Task 2의 상세 절차
- Produces: 개인서버 전용 Codex 작업 완료 루프 문서 세트

- [ ] **Step 1: 참조 경로 확인**

Run: `test -f docs/codex-work-loop.md && rg -n "docs/codex-work-loop\.md" AGENTS.md`

Expected: 상세 문서가 존재하고 `AGENTS.md`의 참조 경로가 정확함.

- [ ] **Step 2: 설계 요구사항 대조**

Run: `rg -n "개인서버|최대 3회|확인 필요|사용자 확인|전역|자동" docs/codex-work-loop.md AGENTS.md`

Expected: 개인서버 한정, 재시도 한도, 검증 불가 처리, 사용자 확인, 전역·자동화 제외 원칙이 확인됨.

- [ ] **Step 3: 최종 diff 확인**

Run: `git diff --check && git status --short && git diff -- AGENTS.md docs/codex-work-loop.md`

Expected: 공백 오류가 없고 문서 파일만 변경됨.

- [ ] **Step 4: 커밋**

Run: `git add AGENTS.md docs/codex-work-loop.md && git commit -m "docs: Codex 작업 완료 루프 반영"`

Expected: 개인서버 전용 작업 완료 루프 규칙이 한글 커밋 메시지로 기록됨. Git 권한 또는 사용자 변경사항으로 커밋이 불가하면 커밋하지 않고 사유를 보고함.
