# CI·로컬 공통 테스트 매트릭스 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CI와 로컬 테스트 실행기가 동일한 서비스별 Python·PYTHONPATH·명령 매트릭스를 사용하게 함.

**Architecture:** JSON 매트릭스를 단일 기준으로 두고 로컬 Python 실행기와 GitHub Actions matrix 생성 Job이 함께 소비함. 각 서비스 테스트는 별도 subprocess로 실행되므로 같은 `app` 패키지명이 충돌하지 않음.

**Tech Stack:** Python stdlib JSON/subprocess/unittest, GitHub Actions YAML.

**Spec:** `docs/superpowers/specs/2026-09-15-ci-test-matrix-design.md`

## Global Constraints

- 서비스 애플리케이션 import와 운영 설정을 수정하지 않음.
- 로컬 기본 실행은 requirements 설치·네트워크·Secret 접근을 수행하지 않음.
- CI와 로컬은 같은 9개 Python 테스트 그룹을 사용함.
- 서비스별 PYTHONPATH는 기존 값과 합치지 않고 덮어씀.

---

### Task 1: 공통 매트릭스와 로컬 실행기

**Files:**
- Create: `tests/ci_test_matrix.json`
- Modify: `tests/run_service_tests.py`
- Modify: `tests/test_run_service_tests.py`

- [ ] **Step 1: 실패 테스트 작성**

매트릭스가 9개 그룹, 정확한 interpreter·PYTHONPATH·명령을 정의하고 `--dry-run`이 subprocess를 실행하지 않음을 검증함.

- [ ] **Step 2: 실패 확인**

Run: `python3 -m unittest tests.test_run_service_tests -v`

Expected: JSON 매트릭스와 `--github-matrix`이 아직 없으므로 FAIL.

- [ ] **Step 3: 최소 구현**

JSON 로딩·스키마 검증, `--list`, `--suite`, `--dry-run`, `--github-matrix`, 그룹별 subprocess 실행과 failure aggregation을 구현함.

- [ ] **Step 4: 통과 확인**

Run: `python3 -m unittest tests.test_run_service_tests -v && python3 tests/run_service_tests.py --dry-run`

Expected: PASS.

### Task 2: GitHub Actions matrix 동기화

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_compose_config.py`

- [ ] **Step 1: 실패 테스트 작성**

CI가 `--github-matrix` 출력에 의존하고 JSON의 9개 그룹을 실행하며 월간 SRE K3s contract를 포함함을 검증함.

- [ ] **Step 2: 실패 확인**

Run: `python3 -m unittest tests.test_compose_config.ComposeConfigTests.test_ci_collects_and_enforces_agent_loop_evidence -v`

Expected: 기존 정적 matrix이므로 FAIL.

- [ ] **Step 3: 최소 구현**

matrix 생성 Job을 추가하고 test Job이 generated output을 사용하도록 전환함. requirements와 추가 패키지는 matrix 필드로 설치함.

- [ ] **Step 4: 통과 확인**

Run: `python3 -m unittest tests.test_compose_config tests.test_run_service_tests -v`

Expected: PASS.

### Task 3: 안전 검증과 문서

**Files:**
- Modify: `docs/agent-handoff.md`
- Test: `tests/test_documentation_index.py`

- [ ] **Step 1: 문서 계약 테스트 작성**

서비스별 matrix 실행기를 CI 등가 로컬 검증 기준으로 명시함.

- [ ] **Step 2: 통합 검증**

Run: `python3 -m unittest tests.test_run_service_tests tests.test_compose_config tests.test_documentation_index -v && python3 tests/run_service_tests.py --dry-run && git diff --check`

Expected: PASS.

### Task 4: 변경 범위와 독립 검토

- [ ] **Step 1: 변경 경로 harness 실행**

Run: `git diff --name-status -z --find-renames origin/main > /tmp/ci-test-matrix-paths.z && python3 scripts/run_change_harness.py --input /tmp/ci-test-matrix-paths.z --input-format git-name-status-z --agent-context`

- [ ] **Step 2: 독립 CI 검토**

matrix source-of-truth, dependency 경계, interpreter/PYTHONPATH 격리, GitHub workflow syntax를 검토함.
