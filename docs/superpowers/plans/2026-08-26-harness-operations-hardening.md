# Harness Operations Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 변경 하네스의 실제 토큰 측정 기록, CI 증적, NUL 입력 계약, 사용·보관 문서를 검증 가능하게 보강함.

**Architecture:** 독립 토큰 측정 스크립트는 JSONL 기록을 검증·집계하고, 변경 하네스는 기존 정책 검증기 입력을 재사용함. CI summary job은 동일 변경 목록으로 두 하네스 출력 형식을 생성해 artifact로 보존함.

**Tech Stack:** Python 3.11 표준 라이브러리, GitHub Actions, unittest, Markdown.

**Spec:** `docs/superpowers/specs/2026-08-26-harness-operations-hardening-design.md`

## Global Constraints

- 서버 기동, 스케줄러, `scripts/deploy-n100.sh`, `.github/workflows/deploy-n100.yml`는 수정하지 않음.
- 모델 토큰은 자동 추정하지 않고 작업자가 수집한 숫자만 검증·집계함.
- 시간 값은 UTC 시간대 인식 ISO 8601 원문을 보존하고 화면·문서에는 KST를 표기함.

---

### Task 1: 토큰 측정 기록 검증·집계

**Files:**
- Create: `scripts/summarize_token_measurements.py`
- Create: `tests/test_token_measurements.py`
- Modify: `scripts/verify_change_scope.py`

**Interfaces:**
- Consumes: JSONL의 `task_id`, `measurement_group`, `model`, `prompt_fingerprint`, `recorded_at`, `baseline_input_tokens`, `baseline_output_tokens`, `harness_input_tokens`, `harness_output_tokens`
- Produces: `measurement_count`, `baseline_total_tokens`, `harness_total_tokens`, `saved_tokens`, `reduction_percent`

- [ ] 작성 전 실패 테스트를 추가하고 측정 스크립트 부재로 실패함을 확인함.
- [ ] UTC 시각·동일 작업 조건·정수 토큰을 검증하는 최소 구현을 추가함.
- [ ] 성공·입력 오류 테스트를 실행함.

### Task 2: 하네스 NUL 입력 계약 및 CI artifact

**Files:**
- Modify: `tests/test_change_harness.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_compose_config.py`

**Interfaces:**
- Consumes: `changed-files.nul`, `--input-format git-name-status-z`
- Produces: `agent-loop-harness.json`, `agent-loop-harness-context.md`

- [ ] rename·공백·한글 경로의 실패 테스트를 추가하고 현재 하네스 CLI의 계약을 확인함.
- [ ] CI summary job에서 구조화·요약 하네스 증적을 생성하고 final artifact에 포함함.
- [ ] CI 계약 테스트를 실행함.

### Task 3: 문서 및 보관 정책 정합성

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/agent-loop-evidence.md`
- Modify: `docs/codex-work-loop.md`

**Interfaces:**
- Consumes: 하네스 CLI와 측정 JSONL 형식
- Produces: 실행 예시, CI/로컬 책임 경계, `.superpowers` 보관 기준

- [ ] 실제 토큰과 UTF-8 바이트 수를 명확히 구분하는 문서 테스트 또는 확인 기준을 추가함.
- [ ] 실행 예시와 artifact 파일명을 실제 CI와 일치시킴.
- [ ] 문서 링크·정적 테스트를 실행함.

### Task 4: 통합 검증 및 독립 검토

**Files:**
- Verify: 변경 파일 전체

- [ ] maintenance 테스트와 변경 범위 검증을 실행함.
- [ ] `git diff --check`와 금지 경로 변경 여부를 확인함.
- [ ] 독립 검토에서 CI·보안 영향과 잔여 위험을 확인함.
