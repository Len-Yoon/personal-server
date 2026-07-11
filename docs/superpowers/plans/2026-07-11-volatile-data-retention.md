# 휘발성 데이터 보존기간 정리 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with review checkpoints.

**Goal:** 뉴스, Obsidian 뉴스, 보안 로그, 백업을 설정된 보존기간 이후 자동 삭제한다.

**Architecture:** 기존 `scripts/maintenance.py`를 단일 정리 진입점으로 확장한다. Obsidian 뉴스는 `YYYY-MM-DD.md` 파일명만 대상으로 별도 함수에서 정리하고, Windows 부트스트랩 데몬은 하루에 한 번 WSL 내부에서 `maintenance.py all`을 호출한다. 기존 뉴스 캐시의 요청 시 purge 동작은 유지한다.

**Tech Stack:** Python 3 표준 라이브러리, PowerShell, Bash, unittest/pytest.

## Global Constraints

- 기본 보존기간은 뉴스 7일, Obsidian 뉴스 30일, 보안 로그 30일, 백업 14일이다.
- 날짜 형식이 `YYYY-MM-DD.md`와 일치하지 않는 Obsidian 파일은 삭제하지 않는다.
- 보존기간은 0일 미만을 허용하지 않는다.
- 정리 실패가 Docker/터널 복구 루프를 중단시키지 않도록 오류를 격리한다.
- 기존 사용자의 미커밋 변경 파일은 수정하거나 스테이징하지 않는다.
- 각 주제는 테스트와 구현을 완료한 뒤 별도 한글 커밋으로 남긴다.

---

### Task 1: 유지보수 스크립트의 Obsidian 뉴스 정리

**Files:**
- Modify: `scripts/maintenance.py`
- Modify: `.env.example`
- Test: `tests/test_maintenance.py`

**Interfaces:**
- `prune_obsidian_news()` reads `OBSIDIAN_VAULT_PATH`, `OBSIDIAN_NEWS_DIR`, and `OBSIDIAN_NEWS_RETENTION_DAYS`.
- CLI accepts `prune-obsidian-news` and includes it in `all`.

- [ ] **Step 1: Write the failing tests**

Add tests using a temporary vault containing an old `2026-06-01.md`, a recent `2026-07-10.md`, and `notes.md`; assert only the old date-named file is removed. Add a test that a negative retention value raises `ValueError`.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `python -m pytest tests/test_maintenance.py -q`
Expected: FAIL because `prune_obsidian_news` and the CLI behavior do not exist.

- [ ] **Step 3: Implement the minimal cleanup behavior**

Add a strict `YYYY-MM-DD.md` matcher, validate non-negative retention, resolve the configured vault/news directory, compare the date in the filename with `datetime.now() - timedelta(days=retention)`, unlink only expired matching files, and add the CLI command.

- [ ] **Step 4: Run focused tests and the maintenance tests again**

Run: `python -m pytest tests/test_maintenance.py -q`
Expected: PASS.

- [ ] **Step 5: Update environment documentation**

Add `OBSIDIAN_NEWS_RETENTION_DAYS=30` to `.env.example` and document the new maintenance command in `README.md` without changing unrelated sections.

- [ ] **Step 6: Commit the topic**

```powershell
git add scripts/maintenance.py tests/test_maintenance.py .env.example README.md
git commit -m "feat: Obsidian 뉴스 보존기간 자동 정리 추가"
```

### Task 2: 자동 정리 실행 연결

**Files:**
- Modify: `scripts/windows-bootstrap.sh`
- Modify: `scripts/windows-bootstrap.ps1`
- Test: `tests/test_windows_bootstrap.py`

**Interfaces:**
- The Bash bootstrap invokes `python3 scripts/maintenance.py all` at most once per calendar day using a marker file under `/tmp`.
- The PowerShell daemon continues its existing 30-minute recovery loop even if maintenance fails.

- [ ] **Step 1: Write the failing tests**

Add a test asserting the Bash bootstrap contains the daily maintenance invocation and marker guard. Add a PowerShell test asserting the daemon calls the WSL maintenance command inside an isolated `try/catch`.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `python -m pytest tests/test_windows_bootstrap.py -q`
Expected: FAIL because the maintenance invocation is not present.

- [ ] **Step 3: Implement the minimal daily invocation**

Add a Bash `run_daily_maintenance` function that compares the current date to `/tmp/personal-server-maintenance.last`, runs `python3 scripts/maintenance.py all`, and updates the marker only after success. Call it after the Compose stack starts. Add the equivalent WSL command to the PowerShell daemon with an independent catch/log message.

- [ ] **Step 4: Run focused tests and the full relevant suite**

Run: `python -m pytest tests/test_windows_bootstrap.py tests/test_maintenance.py -q`
Expected: PASS.

- [ ] **Step 5: Commit the topic**

```powershell
git add scripts/windows-bootstrap.sh scripts/windows-bootstrap.ps1 tests/test_windows_bootstrap.py
git commit -m "feat: 부팅 복구 루프에 일일 정리 연결"
```

### Task 3: 최종 검증 및 운영 문서

**Files:**
- Modify: `docs/n100-mt4-setup.md`
- Test: existing relevant tests only

- [ ] **Step 1: Document defaults and manual commands**

Document the four retention variables and explain that `maintenance.py all` runs automatically once per day after login while manual invocation remains available.

- [ ] **Step 2: Run the full relevant test suite**

Run: `python -m pytest tests/test_maintenance.py tests/test_windows_bootstrap.py tests/crawler_worker/test_news_service.py -q`
Expected: PASS with no unrelated files staged.

- [ ] **Step 3: Commit the topic**

```powershell
git add docs/n100-mt4-setup.md
git commit -m "docs: 데이터 보존기간 운영 방법 문서화"
```
