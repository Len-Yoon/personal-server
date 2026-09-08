# 운영 개선 로드맵 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 완료 계획을 정리하고 안전한 복구 훈련과 실제 토큰 측정 기록을 현재 운영 기준으로 제공함.

**Architecture:** 운영 문서는 현재 상태와 수동 훈련 절차만 제공함. 토큰 수치는 로컬 JSONL에만 기록하고, 기존 집계기는 변경 없이 재사용함.

**Tech Stack:** Markdown, Python 3 standard library, unittest.

**Spec:** `docs/superpowers/specs/2026-09-08-operations-roadmap-design.md`

## Global Constraints

- 서버 기동, scheduler, Portal, K3s 리소스, Caddy, Secret, PVC, 운영 데이터를 수정하지 않음.
- GitHub Actions 공개 상태 감시는 기존 workflow를 유지하고 새 외부 감시 서비스를 추가하지 않음.
- 비밀번호, 토큰, chat ID, Secret 값은 Git·문서·테스트 출력·측정 기록에 포함하지 않음.
- 시간은 기록에서 UTC ISO 8601을 사용하고, 사용자 문서의 표시 시각은 KST `YYYY-MM-DD HH:MM`만 사용함.

### Task 1: 현재 로드맵과 복구 훈련 문서

**Files:**
- Create: `docs/operations-roadmap.md`
- Create: `docs/recovery-drill.md`
- Modify: `docs/README.md`
- Modify: `tests/test_documentation_index.py`

- [x] 현재 구현 완료 항목과 향후 개선 항목을 구분한 로드맵을 작성함.
- [x] 기존 `portal-pvc-backup-verify.sh --check`, `sre-telegram-verify.sh`, `sre-pod-recovery-lab.sh`만 사용하는 격리 복구 훈련 절차를 작성함.
- [x] 문서 색인 계약 테스트를 먼저 추가하고 실패를 확인한 뒤, 문서·색인을 최소 수정함.
- [x] 관련 문서 테스트를 통과시키고 커밋함.

### Task 2: 로컬 토큰 측정 기록 도구

**Files:**
- Create: `scripts/record_token_measurement.py`
- Modify: `tests/test_token_measurements.py`
- Modify: `docs/agent-loop-evidence.md`

- [x] 표준 입력 JSON 한 줄을 검증해 JSONL 파일에 추가하는 실패 테스트를 작성함.
- [x] 실패 원인을 확인한 뒤, UTC·동일 조건·비밀값 없는 기록만 추가하는 최소 CLI를 구현함.
- [x] 기존 `summarize_token_measurements.py`로 생성 기록을 집계하는 테스트를 추가함.
- [x] 토큰 측정 운영 문서와 관련 테스트를 통과시키고 커밋함.

### Task 3: 통합 검증·독립 검토

- [x] 변경 경로 하네스, 문서·토큰 측정 테스트, compileall, diff 검사를 실행함.
- [x] 독립 검토에서 금지 영역·Secret 노출·문서와 실제 도구의 불일치를 확인함.
- [ ] PR 생성·CI 통과 후, 배포 대상이 아님을 확인하고 병합 전 결과를 보고함.
