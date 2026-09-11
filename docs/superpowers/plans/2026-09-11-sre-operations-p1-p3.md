# SRE 운영 고도화 P1~P3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 월간 무중단 복구 훈련 증적, 핵심 공개 서비스 집계 감시, 관리자 읽기 전용 자동복구 이력을 제공함.

**Architecture:** 월간 훈련은 기존 안전 도구를 수동으로 조합하고 결과만 로컬 JSON으로 남김. 외부 감시는 GitHub Actions의 단일 상태 전환을 네 개의 고정 health URL에 적용함. N100 로그는 system-agent에서 정제한 뒤 기존 bridge를 통해 인증된 Portal 화면에 표시함.

**Tech Stack:** Bash, Python unittest, GitHub Actions YAML, FastAPI, Jinja2.

**Spec:** `docs/superpowers/specs/2026-09-11-sre-operations-p1-p3-design.md`

## Global Constraints

- 자동 scheduler·timer·CronJob을 추가하지 않음.
- Portal PVC·Secret·운영 데이터·Caddyfile·Tunnel ingress·Compose Portal writer를 수정하지 않음.
- 자격증명·원본 로그·파일 경로·명령 인수·오류 원문을 상태·UI·증적에 기록하지 않음.
- 공개 health는 네 개의 고정 URL만 점검하고, 장애·복구 Telegram은 단일 전환마다 1회만 유지함.
- 사용자 표시 시간은 KST `YYYY-MM-DD HH:MM`, 내부 저장 시간은 UTC ISO 8601을 사용함.

---

### Task 1: 월간 안전 복구 훈련 실행기와 증적

**Files:**
- Create: `infra/k8s/tools/monthly-recovery-drill.sh`
- Modify: `docs/recovery-drill.md`, `docs/operations-roadmap.md`
- Test: `tests/test_monthly_recovery_drill.py`, `tests/test_documentation_index.py`

- [ ] 실패하는 테스트로 성공 순서, 실패 중단, Pod cleanup, JSON 비밀 비기록을 정의함.
- [ ] 테스트가 새 실행기 부재로 실패함을 확인함.
- [ ] 세 기존 도구만 호출하고 원자 JSON 증적을 남기는 최소 실행기를 구현함.
- [ ] 단위·문서 테스트가 통과함을 확인함.
- [ ] 커밋함.

### Task 2: 네 개 공개 서비스 health 집계 감시

**Files:**
- Modify: `.github/workflows/public-uptime-monitor.yml`, `docs/public-uptime-monitor.md`
- Test: `tests/test_public_uptime_monitor.py`

- [ ] 실패하는 테스트로 네 고정 health URL, all-success 복구, 단일 전환·안전한 실패 식별자 기록을 정의함.
- [ ] 테스트가 기존 단일 URL 계약 때문에 실패함을 확인함.
- [ ] workflow와 문서를 최소 수정해 집계 판정으로 구현함.
- [ ] workflow 계약 테스트가 통과함을 확인함.
- [ ] 커밋함.

### Task 3: system-agent 정제 자동복구 이력 API

**Files:**
- Modify: `system-agent/app/main.py` 및 해당 system-agent 테스트
- Test: `tests/system_agent/test_*.py`

- [ ] 실패하는 테스트로 최근 10개·허용 필드만·손상/부재 시 빈 목록을 정의함.
- [ ] 테스트가 endpoint 부재로 실패함을 확인함.
- [ ] system-agent 내부 endpoint를 구현함.
- [ ] 관련 테스트가 통과함을 확인함.
- [ ] 커밋함.

### Task 4: Portal 관리자 상태 읽기 전용 이력 표시

**Files:**
- Modify: `portal-web/app/services/*`, `portal-web/app/routers/admin.py`, `portal-web/app/templates/admin_status.html`, 필요한 CSS
- Test: `tests/test_portal_dashboard.py`, `tests/test_homeops.py` 또는 새 관리자 상태 테스트

- [ ] 실패하는 테스트로 인증 후 정제 이력·KST 형식·복구 의미 안내를 정의함.
- [ ] 테스트가 새 컨텍스트/표시 부재로 실패함을 확인함.
- [ ] 기존 bridge에서 정제 목록만 가져와 표시하는 최소 구현을 작성함.
- [ ] Portal 관련 테스트가 통과함을 확인함.
- [ ] 커밋함.

### Task 5: 통합 검증·운영 검토

**Files:**
- Modify: 필요한 문서 색인 또는 범위 계약 테스트만

- [ ] 변경 경로로 change harness를 실행하고 maintenance 결과를 반영함.
- [ ] 관련 테스트 묶음, 정적 검사, 독립 운영·보안 검토를 실행함.
- [ ] 적용 전후 공개 health를 10초 간격 3회 확인함.
- [ ] CI 통과 전 push·병합·배포하지 않음.
