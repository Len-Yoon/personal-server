# 컨테이너 non-root 전환 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trivy DS-0002 대상 서비스가 전용 non-root 계정으로 안전하게 실행되도록 전환함.

**Architecture:** 이미지별 UID/GID `10001:10001`을 고정하고, 필요한 writable mount만 명시적으로 준비함. Caddy는 최소 capability만 추가하고, Portal은 K3s securityContext와 copy/restore 경로를 같은 identity로 맞춤.

**Tech Stack:** Docker, Docker Compose, K3s, Bash, Python unittest, Trivy

**Spec:** `docs/superpowers/specs/2026-09-12-nonroot-container-hardening-design.md`

## Global Constraints

- Caddyfile, Tunnel ingress, CronJob schedule/RBAC/Secret 값을 변경하지 않음.
- docker-socket-proxy는 root 예외로 유지하고 Docker socket/group 권한을 다른 서비스에 부여하지 않음.
- Portal PVC·운영 데이터의 일괄 ownership 변경 금지. preflight write probe 실패 시 N100 적용 중단.
- N100 적용은 사용자 승인 후 수동으로만 수행하며, external health 3회 HTTP 200을 확인함.

### Task 1: 이미지 non-root 계약과 Caddy 최소 capability

**Files:** `caddy/Dockerfile`, `car-care-worker/Dockerfile`, `homeops-executor/Dockerfile`, `portal-web/Dockerfile`, `system-agent/Dockerfile`, `docker-compose.yml`, `docker-compose.n100.yml`, `tests/test_compose_config.py`

- [ ] 먼저 Dockerfile/Compose 계약 테스트를 작성해 UID, `COPY --chown`, `USER`, Caddy의 `NET_BIND_SERVICE`, proxy root 예외를 검증함.
- [ ] 대상 5개 이미지에 전용 계정과 명시적 USER를 추가함.
- [ ] car-care writable directory와 Caddy volume 접근 계약을 추가함.
- [ ] 관련 Compose 테스트와 service image build를 통과시킴.

### Task 2: N100 데이터 ownership 준비와 car-care 배포 계약

**Files:** `scripts/deploy-n100-safe.sh`, `tests/test_n100_safe_deployment.py`, `tests/test_compose_config.py`

- [ ] car-care host data directory가 UID/GID 10001로 준비되는 실패 테스트를 작성함.
- [ ] 기존 safe deploy data-directory mapping에 car-care만 추가함.
- [ ] rollback 및 scope classification 계약을 회귀 테스트함.

### Task 3: Portal K3s non-root securityContext와 복원 계약

**Files:** `infra/k8s/tools/portal-cutover.sh`, `infra/k8s/tools/portal-secret-shadow-smoke.sh`, 관련 K3s 테스트

- [ ] Deployment/copy/restore Pod의 root 가정을 검증하는 테스트를 non-root identity 및 PVC write probe 계약으로 교체함.
- [ ] PVC를 연결하는 Deployment/copy/restore Pod에는 동일한 `runAsNonRoot`와 UID/GID만 적용하고 `fsGroup`은 제외함. PVC를 연결하지 않는 shadow smoke emptyDir에만 `fsGroup`을 적용함.
- [ ] SQLite quick_check, 파일 write, rollback 및 manifest render 회귀를 실행함.

### Task 4: 통합 검증과 독립 보안 검토

**Files:** 테스트만 필요 시 수정

- [ ] 대상 서비스 테스트, Compose render, Docker image runtime identity, Trivy config scan을 실행함.
- [ ] 독립 검토가 Caddy capability 최소화, proxy 예외, Portal PVC 비파괴 원칙을 확인함.
- [ ] N100 적용 전 권한 preflight와 rollback 절차를 검토함.
