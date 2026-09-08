# Compose 컨테이너 최소 권한 강화 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** N100 자동 배포 허용 일반 서비스 세 개를 root 없이 실행하고, 기존 데이터 경로·배포 rollback 계약을 유지함.

**Architecture:** 각 Dockerfile은 build 단계에서 전용 UID/GID `10001` 계정을 생성하고 소스를 해당 계정 소유로 복사한 후 비관리 사용자로 전환함. Compose 서비스 정의와 데이터 mount는 변경하지 않으며, 현재 N100 override의 read-only·capability 경계를 그대로 사용함.

**Tech Stack:** Dockerfile, Docker Compose, Python `unittest`, GitHub Actions, N100 safe deployment classifier.

**Spec:** `docs/superpowers/specs/2026-09-07-compose-container-hardening-design.md`

## Global Constraints

- 대상은 `crawler-worker`, `youtube-memo`, `book-memo` Dockerfile로 제한함.
- 각 최종 이미지의 실행 계정은 UID/GID `10001`임.
- `homeops-executor`, Portal, K3s, Caddy, scheduler, Secret·PVC·운영 데이터는 수정하지 않음.
- volume 소유권을 자동 변경하지 않음.
- 실제 N100 배포는 PR CI와 read-only volume 권한 점검을 통과한 뒤 사용자 보고·승인을 받은 경우에만 실행함.

---

## 파일 구조와 작업 경계

| 파일 | 책임 |
|---|---|
| `crawler-worker/Dockerfile` | 뉴스 수집 이미지의 비관리 사용자 실행 |
| `youtube-memo/Dockerfile` | YouTube 메모 이미지의 비관리 사용자 실행 |
| `book-memo/Dockerfile` | 도서 메모 이미지의 비관리 사용자 실행 |
| `tests/test_compose_config.py` | 대상 이미지·제외 이미지의 최소 권한 계약 |

### Task 1: 최소 권한 Dockerfile 계약 추가

**Files:**
- Modify: `tests/test_compose_config.py`

**Interfaces:**
- Consumes: 대상 Dockerfile 경로와 전용 UID/GID `10001`
- Produces: 대상 세 이미지의 계정 생성·소유권 복사·최종 USER 계약

- [ ] **Step 1: 실패 계약 테스트 작성**

```python
for dockerfile in TARGET_DOCKERFILES:
    self.assertIn("addgroup --system --gid 10001 app", contents)
    self.assertIn("adduser --system --uid 10001 --ingroup app app", contents)
    self.assertIn("COPY --chown=10001:10001", contents)
    self.assertIn("USER 10001:10001", contents)
```

- [ ] **Step 2: 실패 확인**

Run: `python3 -m unittest tests.test_compose_config.ComposeConfigTests.test_safe_n100_service_images_run_as_non_root -v`

Expected: 현재 대상 Dockerfile에 전용 사용자 설정이 없어 FAIL.

- [ ] **Step 3: 제외 서비스 고정**

```python
executor = (ROOT / "homeops-executor" / "Dockerfile").read_text(encoding="utf-8")
self.assertNotIn("USER 10001:10001", executor)
```

- [ ] **Step 4: 테스트 재실행**

Run: `python3 -m unittest tests.test_compose_config -v`

Expected: 새 계약은 Dockerfile 구현 전 하나만 FAIL.

### Task 2: 세 서비스 이미지의 전용 사용자 전환

**Files:**
- Modify: `crawler-worker/Dockerfile`
- Modify: `youtube-memo/Dockerfile`
- Modify: `book-memo/Dockerfile`
- Test: `tests/test_compose_config.py`

**Interfaces:**
- Consumes: Task 1의 UID/GID·COPY·USER 계약
- Produces: root 없이 실행되는 세 Docker 이미지

- [ ] **Step 1: 패키지 설치 뒤 전용 계정 생성**

```dockerfile
RUN addgroup --system --gid 10001 app && adduser --system --uid 10001 --ingroup app app
```

- [ ] **Step 2: 애플리케이션 소유권을 명시하여 복사**

```dockerfile
COPY --chown=10001:10001 . .
USER 10001:10001
```

- [ ] **Step 3: 계약 테스트 실행**

Run: `python3 -m unittest tests.test_compose_config -v`

Expected: PASS.

- [ ] **Step 4: 이미지 build 실행**

Run: `docker build -t personal-server-crawler-hardening:local crawler-worker && docker build -t personal-server-youtube-hardening:local youtube-memo && docker build -t personal-server-book-hardening:local book-memo`

Expected: 세 이미지 모두 build PASS.

### Task 3: 변경 범위 검증과 독립 검토

**Files:**
- Verify: 변경된 Dockerfile과 `tests/test_compose_config.py`

**Interfaces:**
- Consumes: Task 1·2 결과
- Produces: 배포 전 검토 가능한 변경·검증 증적

- [ ] **Step 1: 변경 하네스 실행**

Run: `git diff --name-status -z --find-renames origin/main HEAD > /tmp/compose-container-hardening.z && python3 scripts/run_change_harness.py --input /tmp/compose-container-hardening.z --input-format git-name-status-z --agent-context`

Expected: 해당 서비스 검사와 maintenance 검사가 누락 상태로 표시됨.

- [ ] **Step 2: 위험 기반 테스트 실행**

Run: `python3 -m unittest tests.test_compose_config tests.test_n100_safe_deployment -v && python3 -m compileall -q scripts tests && git diff --check origin/main...HEAD`

Expected: PASS.

- [ ] **Step 3: 실제 검사 결과를 하네스에 기록**

Run: `python3 scripts/run_change_harness.py --input /tmp/compose-container-hardening.z --input-format git-name-status-z --check-result crawler-worker=success --check-result youtube-memo=success --check-result book-memo=success --check-result maintenance=success --agent-context`

Expected: `ready_for_review`.

- [ ] **Step 4: 독립 운영·보안 검토 수행**

확인 범위: 대상 외 Dockerfile·Compose·scheduler 변경 없음, Secret 값 미노출, non-root 계약, volume 자동 변경 없음, N100 safe deployment rollback 경계 유지.

- [ ] **Step 5: 커밋**

```bash
git add crawler-worker/Dockerfile youtube-memo/Dockerfile book-memo/Dockerfile tests/test_compose_config.py
git commit -m "security: 자동 배포 서비스 비관리 사용자 실행"
```

## 계획 자체 점검

| 점검 항목 | 결과 |
|---|---|
| Spec 요구사항 | Task 1~3으로 모두 연결됨 |
| Placeholder | 미사용 |
| 타입·경로 일관성 | 대상 Dockerfile·테스트 경로 확인됨 |
| 제외 영역 | executor·Portal·K3s·Caddy·scheduler를 명시적으로 제외함 |
