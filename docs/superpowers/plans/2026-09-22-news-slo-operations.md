# News and SLO Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve crawler notification outbox data while applying the news image, then repair and verify one daily SLO evidence collection without changing persistent infrastructure boundaries.

**Architecture:** Apply the crawler compatibility image before the target image using immutable digest checks and one rollback per stage. First land the SLO query aggregation code and build its collector image. On N100, patch only the existing CronJob container image; preserve the evidence ConfigMap and keep the CronJob suspended after one manual Job validation.

**Tech Stack:** Python unittest, OCI image archive, K3s/containerd, Kubernetes JSON Patch.

**Spec:** `docs/reviews/20260921_뉴스복구호환_적용검토안.md`, `docs/reviews/20260921_SLO증적실패_원인분석.md`, `infra/k8s/README.md`.

## Global Constraints

- Do not change PVC, Secret, Caddy, Tunnel, scheduler configuration, or unrelated workloads.
- Never apply the complete SLO manifest to an existing evidence ConfigMap.
- Keep the SLO CronJob suspended after the one manual Job.
- Do not output archive contents, telemetry payloads, Secret values, or notification outbox entries.
- Stop a stage after one failed rollout or rollback; do not retry blindly.

## Review Focus

- Historical crawler metric label series must aggregate to one scalar and preserve a false freshness result.
- Missing or malformed Prometheus responses must remain unobservable and fail the Job.
- Crawler image changes must test the exact existing image and change only the image field.
- SLO manual Job may update the current date evidence record; evidence history must never be reset or deleted.
- A failed image rollout must retain the previous image and leave automatic SLO scheduling disabled.

---

### Task 1: Aggregate crawler freshness across historical series

**Files:**
- Modify: `infra/k8s/tools/slo-daily-evidence.py:50-54`
- Modify: `tests/test_k8s_slo_daily_evidence.py:274-281`

**Interfaces:**
- Consumes: Prometheus crawler freshness vector from `_QUERY_CRAWLER_FRESHNESS`.
- Produces: one scalar freshness result; no series remains `unobservable` only because labels changed during 24 hours.

- [ ] Write a failing assertion that the query begins with `min(min_over_time(` and retains numeric boolean multiplication.
- [ ] Run `python3 -m unittest tests.test_k8s_slo_daily_evidence.SloDailyEvidenceCollectionTests.test_crawler_query_uses_numeric_bool_multiplication_for_stale_conditions -v` and verify the new assertion fails.
- [ ] Wrap the existing `min_over_time` expression with `min(...)` only.
- [ ] Run `python3 -m unittest tests.test_k8s_slo_daily_evidence -v` and verify all tests pass.
- [ ] Commit with `fix: SLO crawler freshness 시계열 집계 보완`.

### Task 2: Verify and land the SLO source change

**Files:**
- Modify: `docs/superpowers/plans/2026-09-22-news-slo-operations.md`

- [ ] Run the K3s contracts relevant to SLO, SRE audit, and documentation index.
- [ ] Run the change harness with successful `k8s-contracts` and `maintenance` results.
- [ ] Obtain independent review before PR merge; merge only after required CI succeeds.

### Task 3: Apply crawler compatibility then target image

**Files:**
- No repository file change.

- [ ] Read current crawler image, readiness, K3s runtime marker, Docker writer absence, endpoint, and outbox-key existence without printing data.
- [ ] Verify/archive-import the compatibility OCI image, execute one image-only upgrade, and perform required health checks.
- [ ] Verify/archive-import the target OCI image, execute one image-only upgrade using the compatibility image as expected current image, and perform required health checks.
- [ ] On a failed rollout, verify exactly one rollback and stop; never patch PVC, Secret, Caddy, or other Deployment fields.

### Task 4: Apply and validate the SLO collector image

**Files:**
- No repository file change.

- [ ] Read CronJob image, suspend state, active Jobs, evidence ConfigMap shape/count, and target OCI archive metadata without printing records.
- [ ] Build/import the immutable Linux AMD64 collector image and verify its canonical containerd alias.
- [ ] JSON-patch only `spec.jobTemplate.spec.template.spec.containers[0].image` with an exact test-and-replace operation; preserve `suspend: true`.
- [ ] Create one uniquely named manual Job, wait once for completion, then validate only its terminal state and the evidence structural contract.
- [ ] Verify Portal external health three times at ten-second intervals; retain CronJob suspension regardless of result.
