# 보안 P1·P2 하드닝 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 운영 컨테이너 권한 경계와 파일함 자원 제한, 공급망 검증을 보완함.

**Architecture:** 운영 Compose는 이미지 코드만 실행하고 Docker socket proxy가 Docker API를 최소 노출함. 파일함은 요청 전체 자원 한도를 서비스 계층에서 검사하며, 공급망 변경은 고정 버전과 CI 계약으로 검증함.

**Tech Stack:** Docker Compose, FastAPI, Python unittest, GitHub Actions, Dependabot.

**Spec:** `docs/superpowers/specs/2026-09-12-security-p1-p2-hardening-design.md`

## Global Constraints

- N100 실제 배포·Secret·PVC·운영 데이터는 변경하지 않음.
- 현재 허용된 HomeOps 서비스와 재시작 동작을 확장하지 않음.
- 모든 동작 변경은 실패 테스트를 먼저 추가하고 확인함.
- 현재 사용자 작업공간의 미커밋 자동복구 변경을 수정하지 않음.

---

### Task 1: 운영 Compose와 Docker socket 경계

**Files:**
- Modify: `docker-compose.yml`, `docker-compose.n100.yml`, `homeops-executor/app/main.py`, `homeops-executor/app/services/docker_ops.py`
- Modify: `tests/test_compose_config.py`, `tests/homeops_executor/test_docker_ops.py`

- [ ] 운영 override가 코드 bind mount와 reload를 제거하고 HomeOps가 socket proxy만 사용한다는 실패 계약 테스트를 작성·실행함.
- [ ] 최소 Compose·Docker SDK 변경으로 계약을 통과시킴.
- [ ] 관련 HomeOps·Compose 테스트를 실행함.

### Task 2: 파일함 요청 총량 제한

**Files:**
- Modify: `portal-web/app/services/file_store.py`, `portal-web/app/routers/files.py`
- Modify: `tests/test_file_access.py`

- [ ] 다중 업로드·ZIP 총량 초과가 실패하는 테스트를 작성·실행함.
- [ ] 파일 수·누적 바이트 검증과 임시 파일 정리를 구현함.
- [ ] 파일함 테스트를 실행함.

### Task 3: 공급망 고정·검증 자동화

**Files:**
- Modify: `caddy/Dockerfile`, `.github/dependabot.yml`, `.github/workflows/trivy-security.yml`
- Modify: `tests/test_supply_chain_security_workflow.py`, `tests/test_dependabot_config.py`, `tests/test_compose_config.py`

- [ ] Caddy 고정 버전과 Dependabot·Trivy 차단 조건의 실패 계약 테스트를 작성·실행함.
- [ ] 최소 설정 변경으로 계약을 통과시킴.
- [ ] 공급망·Compose 계약 테스트를 실행함.

### Task 4: 범위 및 회귀 검증

**Files:**
- Modify: 관련 테스트·문서만 필요한 경우

- [ ] 변경 경로 하네스를 실행하고 결과를 반영함.
- [ ] 서비스별 관련 테스트, Compose config 검증, Python 컴파일을 실행함.
- [ ] 독립 보안 검토에서 Docker API 권한, 마운트, 업로드 정리, CI 차단 조건을 확인함.
