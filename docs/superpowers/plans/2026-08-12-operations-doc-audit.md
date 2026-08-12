# 운영 문서 정합성 점검 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 현재 운영 구조를 기준으로 사용자와 후속 작업자가 신뢰할 수 있는 문서 체계를 완성함.

**Architecture:** 현재 운영 문서에는 코드·설정의 사실만 반영하고, 과거 QA와 설계·계획 문서는 시간 기준 증적으로 분리함. 문서 색인을 추가해 README에서 단일 진입점을 제공함.

**Tech Stack:** Markdown, Docker Compose, Cloudflare Tunnel, Caddy, GitHub Actions

## Global Constraints

- 서버 기동·스케줄러·Compose 코드는 수정하지 않음.
- 비밀값, 토큰, 비밀번호, 실제 개인 경로는 기록하지 않음.
- 과거 시점 문서의 결론은 삭제하지 않고 현재 운영 문서와 구분함.

---

### Task 1: 현재 운영 문서와 색인 갱신

**Files:**
- Create: `docs/README.md`
- Create: `docs/operations-reference.md`
- Modify: `README.md`, `docs/agent-handoff.md`, `docs/caddy-cloudflare.md`, `docs/cloudflare-tunnel.md`, `docs/n100-mt4-setup.md`, `docs/n100-github-auto-deploy.md`
- Test: Markdown 링크·구성 용어 검증, `git diff --check`

**Interfaces:**
- Consumes: Compose, Caddyfile, bootstrap/deploy scripts, GitHub workflows, `.env.example`
- Produces: 최신 운영 문서 색인과 현재 구성에 맞는 설치·배포·장애 대응 설명

- [x] **Step 1: 서비스·도메인·포트·workflow 기준값을 대조함**
- [x] **Step 2: 현재 운영 문서와 환경변수 참조 문서를 작성함**
- [x] **Step 3: README 및 기존 운영 문서의 오래된 설명을 교체함**
- [x] **Step 4: 링크·구성 용어·Markdown 형식을 검증함**
- [x] **Step 5: 문서 변경을 커밋함**

### Task 2: 과거 증적 문서 상태 표기

**Files:**
- Modify: `docs/20260702_운영보안QA_점검보고서.md`
- Test: 문서 색인과 상태 표기 확인

**Interfaces:**
- Consumes: 문서 작성일과 현재 운영 문서 색인
- Produces: 과거 점검 결과를 보존하면서 최신 기준으로 오인되지 않는 QA 기록

- [x] **Step 1: QA 보고서의 작성일과 기준 구성을 확인함**
- [x] **Step 2: 상단에 기록 상태와 최신 운영 문서 링크를 추가함**
- [x] **Step 3: 링크·Markdown 형식을 검증함**
- [x] **Step 4: 문서 변경을 커밋함**
