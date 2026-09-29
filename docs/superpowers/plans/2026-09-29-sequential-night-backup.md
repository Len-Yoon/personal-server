# Sequential Night Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 매일 02:30 KST에 네 PVC 백업을 앞 Job 종료 후 다음 Job을 시작하는 방식으로 실행함.

**Architecture:** N100 WSL 사용자 timer가 기존 제한된 `sudo -n k3s kubectl`로 호스트 조정기를 실행함. 기존 네 CronJob은 중지하되 Job 템플릿과 자격 증명 경계는 유지하고, Relay는 조정기의 고정 상태 ConfigMap으로 누락·시작 전 실패를 감시함.

**Tech Stack:** Python 표준 라이브러리, systemd user units, Kubernetes Job/CronJob/ConfigMap, 기존 SRE Relay.

**Spec:** [새벽 순차 PVC 백업 설계](../specs/2026-09-29-sequential-night-backup-design.md)

## Global Constraints

- Portal → Book → YouTube → Crawler 순서; 앞 Job의 Complete/Failed 전에는 다음을 시작하지 않음.
- 02:30 KST 시작, 06:00 KST 이후 새로운 Job 생성 금지, `Persistent=false`.
- 기존 Secret·PVC·runner 이미지·ServiceAccount를 보존하고 새 비밀값·sudo 권한을 만들지 않음.
- 기존 네 CronJob은 활성 Job 0건 확인 후 `suspend:true`; 자동배포 대상에서 제외.
- N100 운영 적용은 코드·검증·독립 검토·PR CI 뒤 최종 승인 한 번으로 수행함.

## Review Focus

- Job 생성 응답 손실 뒤 이미 생성된 Job에 재접속하는가.
- Job 비종료·API 불명확 상태에서 다음 백업을 시작하지 않는가.
- runner 시작 전 실패와 timer 미실행이 사용자에게 한 번만 전달되는가.
- Portal 예전 timer 또는 기존 CronJob과 중복 실행할 수 없는가.
- Windows/WSL user manager가 새벽에 실제로 동작하며 재기동 시 낮 백업이 발생하지 않는가.

### Task 1: 순차 실행기와 결정적 Job 수명주기

**Files:** `infra/k8s/tools/pvc-backup-sequence.py`, `tests/test_k8s_pvc_backup_sequence.py`

- [x] 실패하는 순서·중복·응답 손실·시간 경계 테스트를 작성해 확인함.
- [x] 기존 템플릿 검증, Job 생성·재접속·최종 종료 대기, 상태 기록을 구현함.
- [x] 해당 테스트와 구문·diff 검사를 통과함.

### Task 2: Relay 누락·실패 알림

**Files:** `sre-telegram-relay/app/main.py`, `infra/k8s/sre-telegram/pvc-backup-job-reader.yaml`, `tests/test_sre_telegram_relay.py`

- [x] 활성화 기준일 이전 무경보, 실행 누락·단계 실패·정체의 회귀 테스트를 작성해 확인함.
- [x] 고정 ConfigMap 계약과 중복 방지 알림을 구현함.
- [x] 기존 runner 실패 알림과 중복되지 않는지 검증함.

### Task 3: 예약 전환과 운영 기록

**Files:** `infra/k8s/backup-automation/production-cronjob-state.json`, Portal 설치용 manifest, timer/service/state manifest, `infra/k8s/tools/pvc-backup-sequence-automation.sh`, 운영 문서.

- [x] 기존 네 백업 일정·템플릿을 보존하고 운영 목표만 중지로 바꿈.
- [x] 활성 Job·기존 timer·linger·user manager 확인 후에만 새 timer를 활성화하도록 함.
- [x] 사전검토에 롤백·실행 순서·N100 미검증 항목을 기록함.

### Task 4: 통합 검증과 운영 승인 경계

- [ ] 변경 하네스, 관련 테스트와 전체 회귀·문서 검사를 실행함.
- [x] 구현 담당과 독립 운영 검토 담당이 동일 diff를 확인함.
- [ ] 기능 브랜치 PR·CI·Agent Review를 확인함.
- [ ] 승인 전 N100 운영 상태를 변경하지 않고, 최종 승인 이후 별도 배포·다음 정기 실행을 검증함.
