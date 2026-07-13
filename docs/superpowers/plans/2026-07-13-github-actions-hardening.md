# GitHub Actions 배포 안정화 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** GitHub Actions CI와 N100 SSH 자동배포가 실패 조건을 조기에 드러내고 문서·테스트와 일치하도록 안정화한다.

**Architecture:** CI workflow는 저장소 읽기 권한만 사용한다. 배포 workflow는 동일 브랜치의 동시 배포를 직렬화하고 SSH 입력을 검증한다. 서버 스크립트는 배포 디렉터리와 런타임 의존성을 사전 검증한 뒤 원격 main을 반영하고 Compose 설정 검증 후 서비스를 재기동한다.

**Tech Stack:** GitHub Actions YAML, Bash, SSH, Docker Compose, Python `unittest`.

## Global Constraints

- `main` push 배포 트리거는 유지한다.
- 기존 `.env`, `data/`, 운영 서비스 코드와 사용자가 수정한 `scripts/deploy-n100.sh` 실행 권한 변경은 보존한다.
- 배포 스크립트의 `git reset --hard origin/main`은 지정된 배포 디렉터리에서만 실행한다.
- 비밀값은 workflow 로그와 문서에 기록하지 않는다.

### Task 1: 회귀 테스트 작성

**Files:**
- Modify: `tests/test_deploy_n100.py`

- [ ] workflow의 권한·동시성·SSH 옵션과 스크립트의 사전 검증·Compose config 검증을 테스트로 명시한다.
- [ ] 기존 테스트와 새 테스트가 변경 전 코드에서 실패하는지 확인한다.

### Task 2: workflow와 배포 스크립트 보완

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/deploy-n100.yml`
- Modify: `scripts/deploy-n100.sh`

- [ ] CI에 최소 `contents: read` 권한을 지정한다.
- [ ] 배포 workflow에 `concurrency`, 필수 Secret 검증, `StrictHostKeyChecking=yes`를 적용한다.
- [ ] 배포 스크립트에 배포 루트·Git 저장소·`.env`·`data/`·Docker Compose 검증과 `docker compose config`를 추가한다.
- [ ] 새 회귀 테스트를 통과시킨다.

### Task 3: 운영 문서 동기화

**Files:**
- Modify: `docs/n100-github-auto-deploy.md`

- [ ] 실제 Secret 목록, 최초 서버 준비, 수동 사전 검증, Actions 확인 및 실패 복구 절차를 workflow/script와 일치시킨다.
- [ ] 문서 테스트를 통과시킨다.

### Task 4: 전체 검증

- [ ] 배포 관련 테스트와 전체 unittest를 bundled Python으로 실행한다.
- [ ] YAML·Bash 정적 검증 가능 여부를 확인한다.
- [ ] `git diff --check`와 변경 파일을 검토한다.
