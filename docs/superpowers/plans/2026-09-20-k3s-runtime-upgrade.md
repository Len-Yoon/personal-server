# K3s 런타임 업그레이드 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Portal과 Crawler를 immutable digest 이미지로 조건부 교체하고 실패 시 이전 이미지로 한 번 복구함.

**Architecture:** 기존 OCI builder/importer를 재사용함. 새 upgrade tool은 Deployment의 대상 container image field만 JSON Patch로 변경하고 rollout·health 실패 시 이전 image로 rollback함.

**Tech Stack:** Bash, Docker Buildx, OCI archive, K3s/containerd, Kubernetes JSON Patch, Python unittest.

**Spec:** `docs/superpowers/specs/2026-09-20-k3s-runtime-upgrade-design.md`

## Global Constraints

- 대상은 `portal-web`, `crawler-worker`만 허용함.
- target은 `docker.io/library/personal-server-<app>@sha256:<64-hex>`만 허용함.
- `--check`는 읽기 전용이고 `--go`와 `--expected-current-image`가 있어야 patch함.
- PVC·Secret·Caddy·Tunnel·Compose writer·image 이외 Deployment spec은 변경하지 않음.
- rollback은 한 번만 수행함.
- Portal은 외부 health 3회 모두 HTTP 200이어야 성공함.
- 실제 N100 `--go`는 별도 운영 승인 전에는 실행하지 않음.

## Review Focus

- `--check`는 patch·rollout·restart를 호출하지 않아야 함.
- expected image 불일치 시 patch·rollback이 없어야 함.
- target 실패 시 이전 image patch는 정확히 한 번이어야 함.
- Portal probe 세 번 중 비-200이 있으면 성공 처리하지 않아야 함.
- Crawler의 Secret·PVC·Service·non-image spec은 보존되어야 함.

### Task 1: Portal OCI builder 허용

**Files:** Modify `infra/k8s/tools/k3s-app-image-build.sh`; Create `tests/test_k3s_app_image_build.py`.

- [ ] failing test: builder text에 `portal-web`, `--platform linux/amd64`, immutable tag 검증이 있는지 확인함.
- [ ] test 실행: `python3 -m unittest tests.test_k3s_app_image_build -v`; Portal allowlist 부재로 실패해야 함.
- [ ] 구현: builder의 `SUPPORTED_APPS`와 `case`에 `portal-web`을 추가하고 다른 앱·`latest` 거부 규칙은 유지함.
- [ ] test 재실행 후 커밋: `git add infra/k8s/tools/k3s-app-image-build.sh tests/test_k3s_app_image_build.py && git commit -m "feat: Portal K3s 이미지 빌드 허용"`.

### Task 2: image-only upgrade·rollback operator

**Files:** Create `infra/k8s/tools/k3s-app-upgrade.sh`; Create `tests/test_k3s_app_upgrade.py`.

- [ ] fake `sudo`, `kubectl`, `curl`, `ctr` 기반 test fixture를 작성해 call log와 before/after Deployment JSON을 검사함.
- [ ] failing tests: `--check` 무변경, mutable/foreign image 거부, expected image 불일치 중단, target 실패의 one-time rollback, Portal 3회 health, Crawler spec 보존을 작성함.
- [ ] test 실행: `python3 -m unittest tests.test_k3s_app_upgrade -v`; tool 부재로 실패해야 함.
- [ ] 구현: app별 deployment/container/port profile, immutable digest·containerd AMD64·Ready/Endpoint preflight, target image JSON Patch test+replace를 구현함.
- [ ] 구현: target rollout·health 실패 시 시작 image로 한 번 patch하고 rollback rollout·health 실패 시 즉시 실패함. target patch 실패 시 rollback하지 않음.
- [ ] test 및 shell syntax 실행: `python3 -m unittest tests.test_k3s_app_upgrade -v && bash -n infra/k8s/tools/k3s-app-upgrade.sh`.
- [ ] 커밋: `git add infra/k8s/tools/k3s-app-upgrade.sh tests/test_k3s_app_upgrade.py && git commit -m "feat: K3s 앱 안전 업그레이드 도구 추가"`.

### Task 3: CI·문서·scope 연결

**Files:** Modify `tests/ci_test_matrix.json`, `tests/test_run_service_tests.py`, `tests/test_verify_change_scope.py`, `docs/operations-reference.md`.

- [ ] K3s matrix에 `tests.test_k3s_app_image_build`, `tests.test_k3s_app_upgrade`가 포함되는 regression test를 추가함.
- [ ] exact matrix expectation과 generic `infra/k8s/` maintenance scope contract를 검증함.
- [ ] 운영 문서를 build→import→check→별도 승인 go→검증 순서로 갱신함. immutable digest, single rollback, Portal health 3회, 금지 영역을 명시하고 실제 ref·Secret·IP·archive path는 기록하지 않음.
- [ ] 검증: `python3 tests/run_service_tests.py --suite k8s-contracts`, `python3 tests/run_service_tests.py --suite maintenance`, `git diff --check`.
- [ ] 커밋: `git add tests/ci_test_matrix.json tests/test_run_service_tests.py tests/test_verify_change_scope.py docs/operations-reference.md && git commit -m "docs: K3s 런타임 업그레이드 절차 추가"`.

### Task 4: 독립 검토와 live gate

- [ ] NUL-safe change harness를 실행해 maintenance·k8s 검증 결과를 기록함.
- [ ] 독립 검토에서 image-only mutation, immutable validation, rollback count, 금지 영역 비변경, CI matrix를 확인함.
- [ ] source 구현·테스트·병합 뒤 target app을 명시한 별도 승인에서만 N100 image import와 `--go`를 실행함.
