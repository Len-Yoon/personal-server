# A4 Memo Pagination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Book·YouTube 홈 저장 목록을 24건 단위로 조회하고 정확한 페이지 이동을 제공함.

**Architecture:** 각 SQLite 서비스가 총건수와 안정 정렬된 페이지를 조회함. 홈 route가 페이지를 검증하고 템플릿이 탐색 링크를 표시함. 기존 상세·검색 API는 유지함.

**Tech Stack:** Python, FastAPI, Jinja2, SQLite, unittest.

**Spec:** `docs/superpowers/specs/2026-09-27-a4-memo-pagination-design.md`

## Global Constraints

- Crawler·Portal·운영 설정·운영 데이터는 수정하지 않음.
- N100 실제 규모와 성능 개선은 미검증으로 기록함.
- 서비스별 테스트를 별도 프로세스로 실행함.

## Review Focus

- 동률 `updated_at`에서 `id DESC`가 페이지 경계 사이에도 유지되는지 테스트함.
- 빈 목록과 범위 초과 페이지가 일관된 현재 페이지를 보여주는지 테스트함.
- Book 검색어와 책장 편집 후 페이지 문맥을 보존하는지 테스트함.
- Book 페이지 밖 목차를 조회하지 않는지 테스트함.
- 인덱스가 실제 페이지 쿼리에 사용되는지 `EXPLAIN QUERY PLAN`으로 확인함.

---

### Task 1: Book 저장 목록

**Files:** `book-memo/app/services/book_service.py`, `book-memo/app/main.py`, `book-memo/app/templates/home.html`, `book-memo/app/static/css/style.css`, `tests/book_memo/test_book_service.py`, `tests/book_memo/test_ui_contract.py`

**Interfaces:** `list_books_page(page: int, page_size: int = 24) -> tuple[list[dict], int, int]`가 `(목록, 총건수, 보정된 페이지)` 반환함.

- [ ] 경계·정렬·UI 계약 테스트 작성 후 관련 테스트에서 기대한 실패 확인.
- [ ] 페이지 조회와 홈 링크·표시를 구현하고 관련 테스트 통과 확인.
- [ ] 합성 DB 쿼리 계획에서 적용 인덱스를 검증.

### Task 2: YouTube 저장 목록

**Files:** `youtube-memo/app/services/memo_service.py`, `youtube-memo/app/main.py`, `youtube-memo/app/templates/home.html`, `youtube-memo/app/static/css/style.css`, `tests/youtube_memo/test_ui_contract.py`

**Interfaces:** `list_videos_page(page: int, page_size: int = 24) -> tuple[list[dict], int, int]`가 `(목록, 총건수, 보정된 페이지)` 반환함.

- [ ] 경계·정렬·UI 계약 테스트 작성 후 관련 테스트에서 기대한 실패 확인.
- [ ] 페이지 조회와 홈 링크·표시를 구현하고 관련 테스트 통과 확인.
- [ ] 합성 DB 쿼리 계획에서 적용 인덱스를 검증.

### Task 3: 저장소 검증

**Files:** 변경 경로 파일 및 본 계획·설계 문서.

- [ ] 변경 경로 하네스, Book·YouTube 서비스 묶음, 정적 검사, 문서 색인 검사 실행.
- [ ] 실제 결과로 하네스 재실행, diff 확인 후 로컬 커밋.
