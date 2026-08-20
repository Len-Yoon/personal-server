# CI 성공 기반 N100 배포 게이트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CI를 통과한 `main` 코드만 N100에 배포하고 배포 후 서비스 상태를 확인함.

**Architecture:** CI는 PR과 `main` push에서 서비스별 테스트를 수행함. 배포 workflow는 `CI` workflow 완료 이벤트를 수신하고, `main`에서 성공한 실행인 경우에만 Windows self-hosted N100 runner에서 기존 배포 스크립트를 실행함. 이후 workflow가 Docker Compose와 HTTP health endpoint를 확인함.

**Tech Stack:** GitHub Actions, Windows self-hosted runner, WSL2, Bash, Docker Compose, Python unittest.

**Spec:** `docs/superpowers/specs/2026-08-20-ci-cd-deployment-gate-design.md`

## Global Constraints

- `scripts/deploy-n100.sh`, Windows bootstrap, 스케줄러는 수정하지 않음.
- 배포는 `CI` workflow가 `success`이고 `main`에서 발생했을 때만 실행함.
- health check 실패는 workflow 실패로 처리하며 자동 롤백하지 않음.

---

### Task 1: CI 검증 범위 확장

**Files:**
- Modify: `.github/workflows/ci.yml`
- Test: `tests/test_deploy_n100.py`

- [x] 실패 테스트로 CI에 `tests.test_homeops`, `tests.test_homeops_notifier`, `tests.homeops_executor.test_docker_ops`, `tests.crawler_worker.test_news_routes`, `tests.test_deploy_n100`가 포함되는지 확인함.
- [x] portal·crawler-worker·homeops-executor·maintenance CI matrix command를 보완함.
- [x] `python3 -m unittest tests.test_deploy_n100 -v`를 실행함.

### Task 2: CI 성공 배포 게이트와 사후 health check

**Files:**
- Modify: `.github/workflows/deploy-n100.yml`
- Test: `tests/test_deploy_n100.py`

- [x] 실패 테스트로 `workflow_run`, `CI` workflow, `success`, `main`, health check step을 확인함.
- [x] deploy workflow를 `workflow_run` 기반으로 변경하고 `github.event.workflow_run.conclusion == 'success'` 및 `head_branch == 'main'` 조건을 설정함.
- [x] PowerShell에서 Compose 상태와 localhost 서비스·homeops-executor health를 검증함.
- [x] maintenance·deployment 테스트를 실행함.

### Task 3: 전체 검증 및 커밋

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/deploy-n100.yml`
- Modify: `tests/test_deploy_n100.py`
- Create: `docs/superpowers/specs/2026-08-20-ci-cd-deployment-gate-design.md`
- Create: `docs/superpowers/plans/2026-08-20-ci-cd-deployment-gate.md`

- [x] portal·crawler-worker·homeops-executor·maintenance 관련 전체 테스트와 `git diff --check`를 실행함.
- [x] `ci: N100 배포 전 테스트 게이트 추가` 메시지로 커밋함.
