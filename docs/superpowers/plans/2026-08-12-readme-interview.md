# README 면접 관점 개선 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 실제 구현 근거를 유지하면서, 면접관이 프로젝트의 문제 해결력과 작성자 주도 의사결정을 빠르게 파악할 수 있도록 README를 개선함.

**Architecture:** 애플리케이션 코드는 변경하지 않고 `README.md`의 서술 구조만 재구성함. 기존 아키텍처·도메인·보안·CI/CD 설명을 근거로 문제 해결 사례와 의사결정 섹션을 추가함.

**Tech Stack:** Markdown, GitHub Actions, Docker Compose, Cloudflare Tunnel, OpenAI Codex

## Global Constraints

- 확인 가능한 저장소 코드와 운영 문서에 근거한 내용만 작성함.
- 민감한 환경변수·토큰·비밀번호는 문서에 포함하지 않음.
- 서버 기동과 스케줄러 구성은 수정하지 않음.

---

### Task 1: 면접관용 README 서사 보강

**Files:**
- Modify: `README.md:1-145`
- Test: `git diff --check`

**Interfaces:**
- Consumes: 현재 README, `caddy/Caddyfile`, `docs/cloudflare-tunnel.md`, `.github/workflows/ci.yml`, `.github/workflows/deploy-n100.yml`
- Produces: 문제·결정·검증 결과와 작성자 주도 AI 개발 방식을 설명하는 README

- [x] **Step 1: 기존 주장과 근거를 대조함**

Cloudflare Tunnel의 hostname-to-port 규칙, Caddy의 대체 프록시 규칙, CI의 서비스별 테스트 명령, N100 self-hosted 배포 workflow를 근거로 함.

- [x] **Step 2: README 도입과 사례 섹션을 수정함**

도입 문구는 “개인 서버 운영 문제를 직접 설계·구현한 플랫폼”을 먼저 설명하고, Codex는 요구사항 분해·TDD·검토에 사용한 도구로 뒤에 배치함. 사례 표는 아래 열을 사용함.

```markdown
| 문제 | 작성자 의사결정 | 구현·검증 결과 |
|---|---|---|
```

- [x] **Step 3: 의사결정·AI 활용·CI/CD 문구를 보정함**

Cloudflare Tunnel 선택 이유, localhost 바인딩, 역할별 인증, 병합 전 검증 기준을 작성자 결정으로 기술함. Codex 역할은 “구현·검토 역할을 분리한 개발 하네스”로 표현하고, 보안 정책과 병합 판단은 작성자가 수행함을 명시함. CI는 pull request/main 검증, 배포 workflow는 main push 후 N100 재배포로 구분함.

- [x] **Step 4: 문서 검증을 실행함**

Run: `git diff --check`

Expected: 출력 없이 성공함.

- [x] **Step 5: 문서 변경을 커밋함**

```bash
git add README.md docs/superpowers/specs/2026-08-12-readme-interview-design.md docs/superpowers/plans/2026-08-12-readme-interview.md
git commit -m "docs: 면접관 관점의 프로젝트 소개 보강"
```
