# Mini PC Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a lightweight N100 Windows mini PC status dashboard with demo-safe portal summaries and README updates.

**Architecture:** Add a `system-agent` FastAPI service that reads shared data and host metrics JSON. `portal-web` consumes normalized agent data and renders compact status/search sections with `DEMO_MODE` fallback. Existing services get small search APIs.

**Tech Stack:** Python 3, FastAPI, Jinja2 templates, SQLite, Docker Compose, PowerShell, unittest.

---

### Task 1: System Agent Metrics

**Files:**
- Create: `system-agent/app/main.py`
- Create: `system-agent/app/services/metrics.py`
- Create: `system-agent/requirements.txt`
- Create: `system-agent/Dockerfile`
- Test: `tests/system_agent/test_metrics.py`

- [ ] Write failing tests for host metrics, stale warnings, backup status, and demo metrics.
- [ ] Run `PYTHONPATH=system-agent python3 -m unittest tests.system_agent.test_metrics` and confirm it fails because the module does not exist.
- [ ] Implement the agent metrics service and FastAPI routes.
- [ ] Run the test again and confirm it passes.

### Task 2: Compose And Windows Collector

**Files:**
- Modify: `docker-compose.yml`
- Modify: `docker-compose.n100.yml`
- Modify: `.env.example`
- Create: `scripts/windows-host-metrics.ps1`

- [ ] Add `system-agent` to compose with shared read-only data mounts.
- [ ] Add N100 CPU and memory limits.
- [ ] Add environment variables for agent URL, host metrics path, and demo mode.
- [ ] Add a PowerShell collector that writes `data/system/host-metrics.json`.

### Task 3: Portal Status And Search

**Files:**
- Create: `portal-web/app/services/system_status.py`
- Create: `portal-web/app/services/global_search.py`
- Modify: `portal-web/app/routers/dashboard.py`
- Modify: `portal-web/app/templates/dashboard.html`
- Modify: `portal-web/app/static/css/style.css`
- Test: `tests/test_portal_dashboard.py`

- [ ] Write failing tests for demo status and agent failure fallback.
- [ ] Implement portal status fetching, dashboard context, and template sections.
- [ ] Add global search route and grouped results rendering.
- [ ] Run portal tests and confirm they pass.

### Task 4: Service Search APIs

**Files:**
- Modify: `crawler-worker/app/routers/news.py`
- Modify: `youtube-memo/app/main.py`
- Modify: `youtube-memo/app/services/memo_service.py`
- Modify: `book-memo/app/main.py`
- Modify: `book-memo/app/services/book_service.py`

- [ ] Add lightweight JSON search endpoints to news, YouTube memo, and book memo.
- [ ] Keep each endpoint read-only and bounded by a small limit.
- [ ] Reuse existing SQLite query helpers where possible.

### Task 5: Documentation And Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/agent-handoff.md`
- Modify: `docs/n100-mt4-setup.md`

- [ ] Document `system-agent`, `DEMO_MODE`, global search, and Windows Task Scheduler setup.
- [ ] Run `PYTHONPATH=portal-web python3 -m unittest discover -s tests`.
- [ ] Run `PYTHONPATH=system-agent python3 -m unittest tests.system_agent.test_metrics`.
- [ ] Run `python3 -m compileall portal-web/app system-agent/app youtube-memo/app book-memo/app crawler-worker/app`.
