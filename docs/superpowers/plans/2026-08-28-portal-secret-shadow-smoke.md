# Portal Secret Shadow Smoke Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** N100에서 한 명령으로 portal Secret 주입 shadow 검증과 명시적 정리를 수행하는 수동 실행 스크립트를 제공함.

**Architecture:** 스크립트는 K3s Secret 암호화 상태를 확인한 뒤 `.env`에서 실제로 구성된 portal 핵심 키만 허용 목록으로 추출함. 고유 이미지 태그·격리 namespace·외부 노출 없는 Deployment를 사용하고, 키 존재와 `/health`만 확인한 후 성공·실패 모두 대상 리소스를 명시적으로 정리함.

**Tech Stack:** Bash, `sudo k3s kubectl`, K3s containerd, Docker CLI, Python `unittest` 정적 계약 테스트.

**Spec:** 기존 `docs/k3s-flux-transition-draft.md`의 portal first-wave·single-writer·Secret 미평문 원칙 및 2026-08-28 실제 storage smoke 증적.

## Global Constraints

- Compose, Caddy, Flux, scheduler, 기존 portal 컨테이너와 NodePort/Ingress를 변경하지 않음.
- Secret 값·base64·전체 환경을 출력하지 않음. `.env`를 `source`하지 않음.
- Secret 암호화 상태가 Enabled가 아니면 K3s Secret을 만들지 않음.
- shadow는 `emptyDir`, `automountServiceAccountToken: false`, `replicas: 1`, `Recreate`, 고유 이미지 태그를 사용함.
- `EXIT trap`은 Kubernetes 리소스 정리에 사용하지 않으며 namespace·이미지 부재를 명시 확인함.

### Task 1: 정적 계약 테스트 작성

**Files:**
- Create: `tests/test_k8s_portal_secret_shadow_smoke.py`

- [ ] 스크립트가 없어서 실패하는 테스트를 작성함.
- [ ] 안전 경계(암호화 확인, 허용 키, 값 비출력, no `source`, no Service/Ingress/NodePort, 명시 cleanup)를 검사함.

### Task 2: 수동 shadow smoke 스크립트 구현

**Files:**
- Create: `infra/k8s/tools/portal-secret-shadow-smoke.sh`

- [ ] 실제 `.env`를 실행하지 않고 네 개의 현재 구성 핵심 키만 안전하게 추출함.
- [ ] 고유 태그의 portal 이미지를 import하고 shadow Deployment에서 Secret key 존재와 `/health`를 검증함.
- [ ] 실패·성공 모두 namespace와 정확한 containerd image 참조를 정리하고, 결과만 출력함.

### Task 3: 사용 문서와 전체 검증

**Files:**
- Modify: `docs/k3s-flux-transition-draft.md`
- Test: `tests/test_k8s_portal_secret_shadow_smoke.py`

- [ ] N100 일반 WSL 터미널의 한 줄 실행법과 검증 범위·비범위를 기록함.
- [ ] 정적 테스트, shell 구문 검사, 빈 Kustomize 렌더링, change harness를 실행함.
