# P2 감시 경로 식별·알림 보강 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 GitHub Actions 공개 health 감시와 SRE Telegram relay가 실패·복구 상태를 구분해 알리고, 감시 실행 자체가 실패했을 때도 가능한 Telegram 전달과 GitHub Issue 증적을 남김.

**Architecture:** 공개 health workflow는 서비스 상태 전환을 계속 담당함. 새 `workflow_run` 관찰 workflow는 원본 workflow의 완료 결과와 고정된 GitHub API 응답만 읽어 실행 실패 또는 Telegram 전달 실패를 별도 Issue·Telegram 상태로 관리함. 뉴스 수집 알림은 실제 자동 복구기가 없다는 경계를 Telegram 문구에 반영함. 새 외부 감시 서비스, 신규 Secret, N100 운영 변경은 추가하지 않음.

**Tech Stack:** GitHub Actions YAML, GitHub REST API (`actions`, `issues`), Bash, Python `unittest`, Kubernetes SRE Telegram relay Python.

**Spec:** `docs/superpowers/specs/2026-09-16-p2-monitoring-path-design.md`

## Global Constraints

- 기존 `UPTIME_TELEGRAM_BOT_TOKEN`, `UPTIME_TELEGRAM_CHAT_ID`만 참조하며 값·응답 본문·chat ID를 출력 또는 Issue에 저장하지 않음.
- `workflow_run`은 권한 상승 경계임. `actions/checkout`, artifact download, 원본 workflow 코드 실행, 동적 shell 명령 생성을 금지함.
- 알림 workflow는 고정된 API 호출과 고정된 한국어 메시지만 사용하며, 원본 workflow에서 온 문자열은 Issue 분류에 사용하지 않음.
- 원본 공개 health workflow의 정상 서비스 감시·기존 장애/복구 전환 계약은 깨지지 않아야 함.
- GitHub Actions 자체가 예약 실행되지 않는 상태는 새 외부 감시 없이 감지할 수 없음을 문서에 명시함.
- 실제 N100 적용, Secret 생성·변경, GitHub 배포·병합은 본 구현 계획 범위 밖이며 별도 사용자 승인 후에만 수행함.

---

## Task 1: 뉴스 수집 알림의 복구 경계 문구를 정확화

**Files:**
- Modify: `sre-telegram-relay/app/main.py`
- Modify: `tests/test_sre_telegram_relay.py`

- [ ] **Step 1: 실패하는 단위 테스트를 먼저 추가함**

  `test_news_collection_alert_has_korean_secret_free_presentation`에 다음 기대값을 추가함.

  ```python
  self.assertIn("상태: 다음 수집 상태를 확인 중입니다.", message)
  self.assertNotIn("상태: 자동 복구를 확인 중입니다.", message)
  ```

  기존 Portal firing 알림 테스트에는 일반 알림의 문구가 계속 `자동 복구를 확인 중입니다.`인지 유지하는 회귀 기대값을 둠.

- [ ] **Step 2: 테스트가 실패함을 확인함**

  Run: `python3 -m unittest tests.test_sre_telegram_relay.SreTelegramRelayTests.test_news_collection_alert_has_korean_secret_free_presentation`

  Expected: 현재 뉴스 수집 알림이 공통 firing 문구를 사용하므로 신규 기대값에서 실패함.

- [ ] **Step 3: 최소 구현을 추가함**

  `RelayService._format_alert`에서 allow-listed `alertname`이 `NewsCollectionStale`이고 상태가 firing인 경우에만 `다음 수집 상태를 확인 중입니다.`를 선택하는 작은 helper 또는 분기를 추가함. resolved 문구와 다른 alert의 공통 문구는 바꾸지 않음.

- [ ] **Step 4: 관련 relay 테스트를 통과시킴**

  Run: `python3 -m unittest tests.test_sre_telegram_relay`

  Expected: PASS.

- [ ] **Step 5: 변경을 커밋함**

  ```bash
  git add sre-telegram-relay/app/main.py tests/test_sre_telegram_relay.py
  git commit -m "fix: 뉴스 수집 알림 복구 경계 명확화"
  ```

## Task 2: 공개 감시 실행 실패·Telegram 전달 실패 관찰 workflow를 계약 테스트로 고정

**Files:**
- Create: `.github/workflows/public-uptime-monitor-path.yml`
- Modify: `tests/test_public_uptime_monitor.py`
- Modify: `tests/ci_test_matrix.json` (기존 public uptime 계약 테스트가 CI 대상이 아니면 추가; 이미 대상이면 변경하지 않음)

- [ ] **Step 1: 새 관찰 workflow의 실패 계약 테스트를 먼저 추가함**

  `tests/test_public_uptime_monitor.py`에 `MONITOR_PATH_WORKFLOW_PATH`를 추가하고 다음을 검증함.

  ```python
  self.assertIn("workflow_run:", workflow)
  self.assertIn("Public Portal Uptime Monitor", workflow)
  self.assertIn("types: [completed]", workflow)
  self.assertIn("issues: write", workflow)
  self.assertNotIn("actions/checkout", workflow)
  self.assertNotIn("download-artifact", workflow)
  self.assertIn("secrets.UPTIME_TELEGRAM_BOT_TOKEN", workflow)
  self.assertIn("secrets.UPTIME_TELEGRAM_CHAT_ID", workflow)
  ```

  추가로 원본 run의 실패 job/step만 GitHub API로 분류하고, Telegram delivery failure와 일반 execution failure에 각각 고정된 한글 메시지·Issue marker·복구 marker가 있는지 검증함. 비밀값, HTTP 응답 본문, 동적 원본 문자열을 출력하지 않는 계약도 검증함.

- [ ] **Step 2: 테스트가 실패함을 확인함**

  Run: `python3 -m unittest tests.test_public_uptime_monitor.PublicUptimeMonitorTests`

  Expected: workflow 파일이 없으므로 실패함.

- [ ] **Step 3: 안전한 관찰 workflow를 구현함**

  `.github/workflows/public-uptime-monitor-path.yml`을 다음 경계로 작성함.

  1. `on.workflow_run`은 `Public Portal Uptime Monitor`의 `completed`만 수신하고 default branch에서만 동작하게 함.
  2. `permissions`는 `issues: write`와 필요한 읽기 권한만 허용하며 `contents: write`를 주지 않음.
  3. 고정 SHA의 `actions/github-script`로 원본 run의 `monitor` job과 고정된 `Send Telegram status transition` step의 결론만 조회함. 실패 원문·log·artifact·workflow source를 읽거나 실행하지 않음.
  4. 원본 run 실패 시 분류 결과에 따라 `[SRE] 공개 감시 경로 장애` Issue를 열거나 갱신하고, Telegram delivery failure 또는 execution failure용 고정 메시지를 1회 전송함. Telegram 요청의 HTTP 상태와 `.ok == true`만 검증하고 본문은 기록하지 않음.
  5. 관찰 workflow가 후속 원본 run 성공을 확인하면, 이전에 성공적으로 보낸 관찰 장애 알림에 대해서만 고정된 복구 Telegram을 1회 전송하고 Issue를 닫음.
  6. 관찰 workflow의 Telegram 전송 자체가 실패하면 Issue marker는 전송 성공으로 기록하지 않음. 다음 원본 run에서 재시도할 수 있도록 Issue를 열어 둠.
  7. 모든 Issue 본문은 상태 marker, 분류 코드, 안전한 시각/고정 문구만 포함하고 서비스 URL·Secrets·응답 본문을 넣지 않음.

- [ ] **Step 4: 관련 계약 테스트를 통과시킴**

  Run: `python3 -m unittest tests.test_public_uptime_monitor`

  Expected: PASS.

- [ ] **Step 5: CI 매트릭스 계약을 확인함**

  Run: `python3 tests/run_service_tests.py --github-matrix --list | rg 'test_public_uptime_monitor'`

  Expected: public uptime monitor 계약 테스트가 GitHub CI matrix에 정확히 한 번 포함됨.

- [ ] **Step 6: 변경을 커밋함**

  ```bash
  git add .github/workflows/public-uptime-monitor-path.yml tests/test_public_uptime_monitor.py tests/ci_test_matrix.json
  git commit -m "feat: 공개 감시 경로 실패 알림 추가"
  ```

## Task 3: 운영 문서와 로드맵에 경계·점검 절차를 반영

**Files:**
- Modify: `docs/public-uptime-monitor.md`
- Modify: `docs/operations-roadmap.md`
- Modify: `tests/test_documentation_index.py`

- [ ] **Step 1: 문서 계약 테스트를 먼저 추가함**

  `tests/test_documentation_index.py`에 P2 문서가 다음을 명시하는지 검증함.

  - 서비스 상태 알림, 감시 실행 실패, Telegram 전달 실패를 구분함.
  - Telegram 전송이 실패하면 GitHub Issue 증적을 남기고 다음 정상 전송에서 복구를 알림.
  - GitHub Actions 예약 실행 자체가 멈춘 경우는 외부 제공자 없이 감지 범위 밖임.
  - 뉴스 수집 지연은 자동 재시작/자동 복구를 보장하지 않음.

- [ ] **Step 2: 테스트가 실패함을 확인함**

  Run: `python3 -m unittest tests.test_documentation_index.DocumentationIndexTests`

  Expected: P2 문서 경계가 아직 없으므로 실패함.

- [ ] **Step 3: 사용자용 운영 문서를 갱신함**

  `docs/public-uptime-monitor.md`에 감시 경로 상태 표와 수동 확인 절차를 추가함. 메시지 수신자가 알아야 할 영향과 다음 행동을 짧게 쓰되, secret·IP·응답 로그를 노출하지 않음.

  `docs/operations-roadmap.md`는 P2 완료 조건을 반영하고, P3 Cloudflare Tunnel 읽기 전용 drift 점검 및 P4 YouTube Memo 선행 K3s 이전을 후속 후보로 명확히 분리함. 완료로 확정할 수 없는 N100 실제 적용은 `확인 필요`로 둠.

- [ ] **Step 4: 문서 계약 테스트를 통과시킴**

  Run: `python3 -m unittest tests.test_documentation_index`

  Expected: PASS.

- [ ] **Step 5: P2 관련 테스트 묶음을 실행함**

  Run: `python3 -m unittest tests.test_sre_telegram_relay tests.test_public_uptime_monitor tests.test_documentation_index`

  Expected: PASS.

- [ ] **Step 6: 변경 범위 하네스와 정적 검사를 실행함**

  Run: `git diff --name-status -z --find-renames origin/main HEAD > /tmp/p2-monitoring-path-changes.z && python3 scripts/run_change_harness.py --input /tmp/p2-monitoring-path-changes.z --input-format git-name-status-z --agent-context`

  Expected: 변경 경로와 요구 검증이 표시되고, P2 관련 검사 결과를 `--check-result`로 다시 기록할 수 있음.

- [ ] **Step 7: 변경을 커밋함**

  ```bash
  git add docs/public-uptime-monitor.md docs/operations-roadmap.md tests/test_documentation_index.py
  git commit -m "docs: 공개 감시 경로 점검 기준 보완"
  ```

## Task 4: 독립 검토와 완료 전 검증

**Files:**
- Review: Task 1~3 변경 파일 전체

- [ ] **Step 1: 독립 검토를 요청함**

  구현과 다른 에이전트가 diff를 검토해 다음을 확인함.

  - `workflow_run`에 checkout, artifact download, 원본 실행이 없음.
  - 권한과 Secret 사용 범위가 최소임.
  - 원본 공개 health workflow의 상태 전환 계약을 변경하지 않음.
  - Telegram 실패 시 성공 marker/복구 Issue가 잘못 기록되지 않음.
  - 뉴스 수집 알림이 실제 자동 복구를 암시하지 않음.

- [ ] **Step 2: 최종 검증을 실행함**

  Run: `python3 -m unittest tests.test_sre_telegram_relay tests.test_public_uptime_monitor tests.test_documentation_index`

  Expected: PASS.

- [ ] **Step 3: 검증 결과를 하네스에 반영함**

  Run: `python3 scripts/run_change_harness.py --input /tmp/p2-monitoring-path-changes.z --input-format git-name-status-z --check-result p2_contracts=success --agent-context`

  Expected: required checks complete 상태.

- [ ] **Step 4: 사용자에게 병합·배포 결정을 요청함**

  테스트와 독립 검토가 통과한 경우에만 변경 요약, 미검증 실제 GitHub 실행, GitHub/N100 배포가 별도임을 보고하고 병합·적용 승인을 요청함.

## Self Review Checklist

- [ ] Task마다 테스트 선행, 실패 확인, 최소 구현, 재검증 순서가 있음.
- [ ] `workflow_run` privilege boundary와 신뢰할 수 없는 입력 금지 경계를 검증함.
- [ ] 새 외부 모니터링 제공자·Secret·N100 운영 변경을 추가하지 않음.
- [ ] 기존 공개 health 및 SRE relay 회귀 테스트를 포함함.
- [ ] 실제 GitHub Actions 예약 미실행 감지는 범위 밖임을 문서화함.
