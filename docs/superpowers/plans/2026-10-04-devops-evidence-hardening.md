# DevOps 증거 고도화 Implementation Plan

> **For agentic workers:** 기존 승인·분업은 AGENTS.md를 따름. 각 담당은 TDD로 구현하고 전문 담당이 전체 변경을 독립 검토함.

**Goal:** Loki·실패 배포·Ansible drift·SLO의 재현 가능한 검증을 강화함.
**Architecture:** 기존 실습 운영 도구와 localhost playbook을 확장함. SLO는 별도 읽기 전용 집계기를 제공하며, 재현된 HTTP 403에 대해서만 기존 수집기의 공개 요청 식별자를 최소 보완함. 운영 이미지·일정·증적은 변경하지 않음.
**Tech Stack:** Bash, Python unittest, Ansible, Docker Compose, K3s.
**Spec:** [설계](../specs/2026-10-04-devops-evidence-hardening-design.md).

## Global Constraints

- 기준 `204c3ff`, 기능 브랜치 `codex/devops-evidence-hardening`.
- 운영 앱·기존 PVC 데이터·Secret·scheduler·bootstrap 변경 금지.
- 병렬 담당 파일을 분리함. git commit·matrix·공용 문서는 주 담당만 수정함.
- 실제 변경 승인과 읽기 전용 검증을 분리함.

## Review Focus

1. retry 횟수·개별 kubectl timeout·최종 실패 보고의 상한.
2. 실패 훈련 중 interrupt/부분 적용 시 복구 및 소유권 확인.
3. drift 원본 파일 symlink·새 파일·예상 밖 Compose 자원 방어.
4. SLO 중복 날짜·누락·관측 불가·기간 경계·무트래픽 처리.
5. 성공률과 coverage 구분 및 일일 증거를 실제 시간 기반 SLO로 과장하지 않는 계약.

## Task 1: Loki 검증·실패 배포 훈련

**Files:** `infra/k8s/tools/observability-lab.sh`, 실습 전용 새 훈련 도구·manifest, `tests/test_k8s_observability_lab_tools.py`, `tests/test_k8s_observability_lab.py`.
**Interfaces:** 고정 context 검증 후 `--verify`; 새 훈련은 read-only check 및 명시 실행 모드. 주 담당이 CI matrix에 신규 테스트를 등록함.

- [x] RED: 지연된 샘플 로그, 지속 실패, context 불일치, 다른 소유 자원, interrupt/복구 실패를 재현하는 테스트 작성·실패 확인.
- [x] GREEN: 제한된 retry와 실습 전용 실패·복구 경로 구현.
- [x] 관련 unittest·Bash 구문·보호 경계 검증.

## Task 2: Ansible drift

**Files:** `infra/ansible-lab/` 내 전용 drift 도구, `tests/test_ansible_lab_contract.py`.
**Interfaces:** 기존 site·rollback 및 고정 소유 project/경로 사용, 원본 response만 변경. 공용 matrix 수정 금지.

- [x] RED: drift 감지→복구→changed=0, 다른 소유 파일·symlink·오류 시 원복 테스트.
- [x] GREEN: dry-run과 명시 실행·안전 복구 구현.
- [x] 계약 검사·Ansible syntax-check 및 로컬 실제 파일·자식 프로세스 리허설 수행. N100 Docker 실습은 운영 승인 후 수행함.

## Task 3: SLO 읽기 전용 집계

**Files:** `infra/k8s/tools/slo-evidence-summary.py`, `tests/test_k8s_slo_evidence_summary.py`, `docs/slo-baseline.md`.
읽기 전용 진단의 HTTP 403 원인 재현에 따라 `slo-daily-evidence.py` 공개 GET의 고정 User-Agent만 추가하고 기존 테스트에서 실제 403 failed 유지와 요청 헤더를 확인함. 운영 이미지·CronJob은 수정하지 않음.
**Interfaces:** `summarize_records(records, end_date, days=30)`와 `--input FILE --end-date YYYY-MM-DD --days N` CLI. records.json 배열 또는 고정 ConfigMap JSON 입력만 소비함.

- [x] RED: 완전·부분 관측, 실패, 미수집, 잘못된 입력, duplicate, 기간 경계, empty 트래픽 테스트.
- [x] GREEN: 고정 필드 집계·coverage·가중 요청 오류 비율 산정 및 개인정보 없는 JSON 출력 구현.
- [x] unittest 및 실제 기존 ConfigMap 읽기 전용 자료 집계. 30일 부족은 부족 상태로 보고.

## Task 4: 통합·검토·반영

- [x] 새 테스트의 CI matrix 등록과 정확히 1회 배정 검사.
- [x] 관련 전체 묶음·문서·harness 및 독립 전문 검토. 최종 전체 9개 묶음 1,895건 통과, PR·main CI는 별도로 확인함.
- [ ] 한 PR로 커밋·push·CI·Agent Review·병합·main 검사.
- [x] 운영 승인 준비: 대상, 실행 순서, 보존 대상, before/after 검증을 검증 보고서에 기록함. 최종 SHA는 병합 후 제시함.
- [x] 포트폴리오·로드맵에 실제 검증과 미수행을 분리 기록함.
