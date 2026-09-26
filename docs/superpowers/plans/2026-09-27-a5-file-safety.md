# A5 Portal File Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 파일함의 정적 symlink 외부 접근과 업로드 파싱 전 무제한 요청 수신을 차단함.

**Architecture:** `file_store.py`가 직접 경로와 재귀 항목을 검사함. `main.py`의 순수 ASGI 미들웨어가 두 업로드 요청을 multipart 파서 전에 제한함.

**Tech Stack:** Python 3.11, FastAPI/Starlette, unittest.

**Spec:** `docs/superpowers/specs/2026-09-27-a5-file-safety-design.md`

## Global Constraints

- Caddy·Tunnel·K3s·PVC·운영 데이터·배포 정책을 변경하지 않음.
- 사용자 승인에 따라 설계·계획 검토 대기를 추가하지 않음.
- 신규 테스트 RED를 확인하고 최소 구현으로 GREEN을 확인함.

## Review Focus

- 저장소 밖 파일 symlink가 ZIP에 포함되지 않음: Task 1 테스트.
- 저장소 밖 폴더 symlink가 삭제 범위를 넓히지 않음: Task 1 테스트.
- 경로 구성요소 symlink가 직접 접근에서 거부됨: Task 1 테스트.
- Content-Length 없는 초과 요청도 413으로 종료됨: Task 2 테스트.
- 정상 크기 두 업로드 경로는 303과 저장을 유지함: Task 2 테스트.

---

### Task 1: 파일 경로와 재귀 작업

**Files:** `portal-web/app/services/file_store.py`, `portal-web/app/routers/files.py`, `tests/test_file_access.py`, `tests/test_portal_security.py`

**Interfaces:** `file_store._safe_path(relative_path: str) -> Path`, `file_store.validate_download_limits(relative_paths: list[str]) -> None`, 재귀 파일 검사 iterator.

- [x] 목록·직접 경로·재귀 ZIP·재귀 삭제 회귀 테스트 작성.
- [x] 신규 테스트 RED 확인.
- [x] 경로 구성요소와 재귀 항목 검사, ZIP·삭제 전 검사 구현.
- [x] 신규 테스트 GREEN 확인.

### Task 2: 업로드 수신 제한

**Files:** `portal-web/app/main.py`, `tests/test_file_access.py`

**Interfaces:** 두 POST 경로만 적용하는 ASGI 미들웨어; Content-Length와 receive 누적 초과 시 413.

- [x] 단일·다중 Content-Length 초과, 길이 미상 스트림 초과, 정상 요청 테스트 작성.
- [x] 신규 테스트 RED 확인.
- [x] 각 저장 상한에 multipart 여유 1 MiB를 더한 수신 제한 구현.
- [x] 신규 테스트 GREEN 확인.

### Task 3: 검증과 인계

**Files:** 위 파일과 설계·계획 문서.

- [x] `python3 tests/run_service_tests.py --suite portal` 및 `--suite maintenance`, 구문·diff 검증 실행.
- [x] 하네스에 실제 결과 입력 및 독립 보안 검토 인계.
- [x] 기능 브랜치 로컬 커밋 생성; push·PR은 통합 담당에게 인계.

### 독립 보안 검토 보완

- [x] ZIP 검사·`archive.write(path)` 사이 symlink 교체, 삭제 검사 후 교체·부분 삭제를 RED로 재현함.
- [x] ZIP 실제 읽기를 `O_NOFOLLOW` FD로 고정하고, 작성 중 파일 수·바이트 상한을 재검사함.
- [x] 재귀 삭제를 FD 기반 사전 검사·삭제로 바꾸고 외부 symlink 대상을 따라가지 않음.
- [x] 업로드 전체 사전 버퍼를 제거하고 스트리밍 바이트 상한 및 413 변환을 유지함.
- [x] Portal·maintenance·하네스 재검증 및 독립 검토에 재인계함.
