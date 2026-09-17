# Book Memo K3s Runtime Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** K3s로 운영 전환된 Book Memo가 Git 동기화, bootstrap, 안전 자동배포로 Docker writer로 되돌아가지 않게 함.

**Architecture:** 신뢰된 런타임 상태 파일의 `book-memo=k3s`를 단일 판단 기준으로 사용함. K3s 모드에서는 Compose build/up, Docker health, Docker rollback 재개를 거부하고, health 검증은 K3s Deployment·Endpoint·Service 및 공개 Books 상태를 사용함. ClusterIP는 Git에 고정 저장하지 않음.

**Tech Stack:** Bash, Python unittest, Docker Compose, K3s kubectl, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-16-k3s-app-migration-design.md`

## Global Constraints

- 현재 K3s Book Memo, PVC, Docker 롤백 데이터와 지정 백업은 변경·삭제하지 않음.
- Caddy/Tunnel 실제 적용은 이 계획의 범위 밖이며, 저장소 정리는 현재 운영 상태를 되돌리지 않아야 함.
- K3s Service ClusterIP는 환경에서 조회하며 Git·문서에 값으로 저장하지 않음.

---

### Task 1: K3s 모드의 Docker 재기동 차단

**Files:**
- Modify: `scripts/deploy-n100-safe.sh`
- Modify: `scripts/verify-n100-safe-deployment-health.sh`
- Test: `tests/test_n100_safe_deployment.py`

**Interfaces:**
- Consumes: root-owned runtime state의 `book-memo=k3s`
- Produces: K3s 소유 Book Memo의 Docker build/up/rollback을 수행하지 않는 안전 배포 결과

- [ ] 실패 테스트: K3s 상태의 Book Memo 변경이 Docker build/up/rollback 경로를 호출하지 않음을 검증함.
- [ ] 실패 확인: 해당 테스트가 기존 무조건 Docker 경로 때문에 실패함을 확인함.
- [ ] 최소 구현: safe deploy와 health 검증이 K3s 소유 Book Memo를 fail-closed로 제외하도록 수정함.
- [ ] 통과 확인: 단위 테스트와 관련 safe-deploy 테스트를 실행함.

### Task 2: 일반 배포 health의 K3s 계약 보완

**Files:**
- Modify: `scripts/verify-n100-deployment-health.sh`
- Modify: `tests/test_deploy_n100.py`

**Interfaces:**
- Consumes: runtime service mode와 K3s Service/Endpoint 상태
- Produces: K3s 모드에서 Docker loopback health를 요구하지 않는 검증 결과

- [ ] 실패 테스트: `book-memo=k3s`에서 Docker loopback URL 검사가 발생하지 않음을 작성함.
- [ ] 실패 확인: 기존 무조건 loopback 검사로 실패함을 확인함.
- [ ] 최소 구현: K3s mode는 Deployment ready, Endpoint ready, Service port 및 공개 health로 검증함.
- [ ] 통과 확인: 배포 관련 테스트를 실행함.

### Task 3: Compose 의존성과 문서 정합성

**Files:**
- Modify: `docker-compose.n100.yml`
- Modify: `docs/cloudflare-tunnel.md`
- Modify: `docs/operations-reference.md`
- Test: `tests/test_compose_config.py`
- Test: `tests/test_documentation_index.py`

**Interfaces:**
- Consumes: Book Memo runtime state와 현재 Tunnel → Caddy → K3s 운영 구조
- Produces: Caddy가 Compose Book Memo healthy 의존성을 갖지 않고, 문서가 롤백 자산 보존을 명시함

- [ ] 실패 테스트: N100 Caddy가 Compose `book-memo`에 의존하지 않음을 작성함.
- [ ] 실패 확인: 기존 의존성으로 실패함을 확인함.
- [ ] 최소 구현: Caddy의 해당 의존성만 제거하고, Docker service 정의와 롤백 데이터는 보존함.
- [ ] 문서 수정: 현재 운영 경로, 보존 자산, 정적 K3s manifest 재적용 금지를 기록함.
- [ ] 통과 확인: Compose·문서·전환 회귀 테스트를 실행함.

### Task 4: 통합 검증과 독립 검토

- [ ] 변경 경로 하네스와 정적 검사를 실행함.
- [ ] 관련 단위 테스트 묶음을 실행함.
- [ ] 실제 N100 상태는 읽기 전용으로 재확인함.
- [ ] 독립 검토에서 Docker 재기동·PVC·백업 자산·공개 경로 회귀가 없음을 확인함.
- [ ] 사용자 승인 후에만 커밋·PR·병합·N100 적용을 진행함.
