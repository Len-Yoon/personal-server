# Security Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Protect all write operations, strengthen browser session controls, and persist brute-force defenses without changing startup or scheduler files.

**Architecture:** Add small, service-local security helpers rather than cross-container shared runtime code. Each public FastAPI service applies the same response headers and Origin guard; each state-changing feature calls its existing password policy through a session-aware guard. Rate-limit persistence uses atomic JSON files on existing writable data mounts.

**Tech Stack:** FastAPI, Starlette responses, Python standard library (`secrets`, `json`, `os.replace`), unittest.

## Global Constraints

- Do not change server startup or scheduler files.
- Keep existing public read endpoints and UI layout functional.
- Use `DELETE_PASSWORD` as the existing write-operation credential.
- Use TDD: observe each new behavior fail before production implementation.
- Commit messages use Korean `유형: 설명` form.

---

### Task 1: Shared portal session and policy hardening

**Files:**
- Modify: `portal-web/app/services/security.py`, `portal-web/app/routers/files.py`, `portal-web/app/routers/portfolio.py`
- Modify: `tests/test_file_access.py`, `tests/test_portfolio.py`, `tests/test_portal_security.py`

- [ ] Add failing tests for Secure cookies in production, random session rejection after restart, and persisted rate-limit records.
- [ ] Run the focused tests and confirm they fail because cookie/session persistence behavior is absent.
- [ ] Implement a bounded session store, atomic rate-limit store, and actual file-manager environment policy.
- [ ] Re-run focused tests and commit portal security changes.

### Task 2: Public-service browser protection

**Files:**
- Modify: `crawler-worker/app/main.py`, `youtube-memo/app/main.py`, `book-memo/app/main.py`
- Create: service-local security helpers where needed
- Test: `tests/crawler_worker/test_news_routes.py`, `tests/youtube_memo/test_ui_contract.py`, `tests/book_memo/test_ui_contract.py`

- [ ] Add failing tests that assert CSP and related headers on each public service.
- [ ] Add the minimal middleware that applies headers and rejects cross-origin unsafe requests.
- [ ] Run focused tests and commit header/CSRF changes.

### Task 3: Protect book and YouTube write paths

**Files:**
- Modify: `youtube-memo/app/main.py`, `book-memo/app/main.py`, related templates
- Test: `tests/youtube_memo/test_ui_contract.py`, `tests/book_memo/test_ui_contract.py`

- [ ] Add failing tests showing unauthenticated content creation and modification are rejected.
- [ ] Add per-service sign-in and session guards while retaining existing deletion password prompts.
- [ ] Update forms to submit same-origin CSRF-compatible write requests.
- [ ] Run focused tests and commit write protection.

### Task 4: Environment documentation and regression verification

**Files:**
- Modify: `.env.example`, `README.md`
- Test: existing portal, book, YouTube, crawler, system-agent, and browser-client suites

- [ ] Document the active file-manager policy and optional rate-limit state path.
- [ ] Run all project tests and `git diff --check`.
- [ ] Request a final code review, fix valid findings, then commit the final documentation.
