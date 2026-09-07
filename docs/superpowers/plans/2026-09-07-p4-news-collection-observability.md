# 뉴스 수집 장애 관측성 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 뉴스 수집 실패·지연을 정확히 감지해 기존 SRE Telegram 경로로 장애와 복구를 전달함.

**Architecture:** crawler-worker 정기 수집은 성공·실패 상태를 영속 파일로 기록하고, 인증된 내부 `/internal/metrics`가 해당 상태를 Prometheus 형식으로 노출함. K3s의 `compose-crawler` bridge Service를 ServiceMonitor가 수집하고 PrometheusRule이 Alertmanager 및 SRE relay에 전달함.

**Tech Stack:** Python/FastAPI, Prometheus Operator, Alertmanager, Kubernetes manifests, unittest.

**Spec:** `docs/superpowers/specs/2026-09-07-news-collection-observability-design.md`

## Global Constraints

- 서버 기동 구성, Caddy, Compose bridge 구성, 스케줄 실행 주기는 수정하지 않음.
- 승인된 예외로 뉴스 스케줄러와 수집 예외 경로에 상태 기록만 추가함.
- Secret 값·기사 내용·URL·예외 원문을 Git, 로그, 지표, 알림에 기록하지 않음.
- 실제 Kubernetes 적용, 배포, Secret 생성은 수행하지 않음.
- 모든 시각은 내부적으로 UTC 시간대 인식 ISO 8601로 저장함.

---

### Task 1: 수집 상태 기록과 정기 수집 실패 계약

**Files:**
- Create: `crawler-worker/app/services/news_collection_status.py`
- Modify: `crawler-worker/app/services/news_archive.py`, `crawler-worker/app/services/news_scheduler.py`
- Test: `tests/crawler_worker/test_news_collection_status.py`, `tests/crawler_worker/test_news_scheduler.py`, `tests/crawler_worker/test_news_service.py`

**Interfaces:**
- Produces: `NewsCollectionStatusStore`, `record_attempt`, `record_success`, `record_failure`, `snapshot`.
- Consumes: `collect_korean_news(..., raise_on_source_error=True)` from scheduler only.

- [ ] Write failing tests for source exception, empty successful collection, restart-safe status, and secret-free snapshot.
- [ ] Run focused tests and confirm expected failure because status API and strict collection path do not exist.
- [ ] Add the smallest atomic JSON status store and route scheduler success/failure through it.
- [ ] Run focused crawler tests and confirm pass.

### Task 2: 인증된 내부 metrics endpoint

**Files:**
- Create: `crawler-worker/app/services/news_collection_metrics.py`
- Modify: `crawler-worker/app/main.py`
- Test: `tests/crawler_worker/test_news_collection_metrics.py`, `tests/crawler_worker/test_news_routes.py`

**Interfaces:**
- Consumes: status snapshot and `NEWS_METRICS_BEARER_TOKEN`.
- Produces: `GET /internal/metrics` with `text/plain; version=0.0.4` only for a valid bearer token.

- [ ] Write failing tests for authorized exposition and 404 for absent or wrong credentials.
- [ ] Run focused tests and confirm expected failure because endpoint does not exist.
- [ ] Implement minimal exposition with fixed metric names and no user-controlled labels.
- [ ] Run focused crawler tests and confirm pass.

### Task 3: Prometheus, Alertmanager relay, and contracts

**Files:**
- Create: `infra/k8s/sre-telegram/crawler-news-observability.yaml`
- Modify: `infra/k8s/sre-telegram/prometheus-rule.yaml`, `sre-telegram-relay/app/main.py`
- Test: `tests/test_k8s_sre_telegram_manifests.py`, `tests/test_sre_telegram_relay.py`

**Interfaces:**
- Consumes: authenticated `/internal/metrics`, existing `compose-crawler` service, existing Alertmanager `sre_telegram="true"` routing.
- Produces: `ServiceMonitor` and `NewsCollectionStale` rule plus Korean relay presentation.

- [ ] Write failing manifest and relay formatting tests.
- [ ] Run focused tests and confirm expected failure because the metric ServiceMonitor/rule and presentation do not exist.
- [ ] Add a ServiceMonitor referencing only a named Secret key, and an alert rule with 15-minute startup/staleness guard and 3 consecutive failures threshold.
- [ ] Run focused K3s and relay tests and confirm pass.

### Task 4: 통합 검증과 문서

**Files:**
- Modify: `docs/operations-reference.md`, `docs/README.md`
- Test: relevant crawler, relay, and K3s contracts

- [ ] Document key names only and apply/rollback verification sequence; do not include values.
- [ ] Run `git diff --check`, focused test suites, and the change harness with recorded success results.
- [ ] Perform independent diff review before commit.
- [ ] Commit only scoped changes using a Korean commit message.
