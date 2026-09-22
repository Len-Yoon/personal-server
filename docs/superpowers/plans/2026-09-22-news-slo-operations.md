# News and SLO Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve crawler notification outbox data while applying the news image, then repair and verify one daily SLO evidence collection without changing persistent infrastructure boundaries.

**Architecture:** Apply the crawler compatibility image before the target image using immutable digest checks and one rollback per stage. First land the SLO query aggregation code and build its collector image. On N100, patch only the existing CronJob container image; preserve the evidence ConfigMap and keep the CronJob suspended after one manual Job validation.

**Tech Stack:** Python unittest, OCI image archive, K3s/containerd, Kubernetes JSON Patch.

**Spec:** `docs/reviews/20260921_뉴스복구호환_적용검토안.md`, `docs/reviews/20260921_SLO증적실패_원인분석.md`, `infra/k8s/README.md`.

## Global Constraints

- Do not change PVC, Secret, Caddy, Tunnel, schedule/time zone, or unrelated workloads. The live SLO CronJob is first set to `suspend: true` for the approved manual validation and remains suspended afterward.
- Never apply the complete SLO manifest to an existing evidence ConfigMap.
- Keep the SLO CronJob suspended after the one manual Job until a separately approved activation.
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

- [x] Write a failing assertion that the query begins with `min(min_over_time(` and retains numeric boolean multiplication.
- [x] Run the focused regression test and verify the new assertion fails before the implementation.
- [x] Wrap the existing `min_over_time` expression with `min(...)` only.
- [x] Run `python3 -m unittest tests.test_k8s_slo_daily_evidence -v` and verify all tests pass.
- [x] Commit with `fix: SLO crawler freshness 시계열 집계 보완`.

### Task 2: Verify and land the SLO source change

**Files:**
- Modify: `docs/superpowers/plans/2026-09-22-news-slo-operations.md`

- [x] Run the K3s contracts relevant to SLO, SRE audit, and documentation index.
- [x] Run the change harness with successful `k8s-contracts` and `maintenance` results.
- [x] Obtain independent review before PR merge; merge only after required CI succeeds.

### Task 3: Apply crawler compatibility then target image

**Files:**
- No repository file change.

- [x] Read current crawler image, readiness, K3s runtime marker, Docker writer absence, endpoint, and outbox-key existence without printing data.
- [x] Verify/archive-import the compatibility OCI image, execute one image-only upgrade, and perform required health checks.
- [x] Verify/archive-import the target OCI image, execute one image-only upgrade using the compatibility image as expected current image, and perform required health checks.
- [x] No rollout failed; PVC, Secret, Caddy, and other Deployment fields were not patched.

### Task 4: Apply and validate the SLO collector image

**Files:**
- No repository file change.

- [x] Read CronJob image, suspend state, active Jobs, evidence ConfigMap shape/count, and target OCI archive metadata without printing records.
- [x] Build/import the immutable Linux AMD64 collector image and verify its canonical containerd alias.
- [x] JSON-patch only `spec.jobTemplate.spec.template.spec.containers[0].image` with an exact test-and-replace operation; preserve `suspend: true`.
- [x] Create one uniquely named manual Job, wait once for completion, then validate only its terminal state and the evidence structural contract.
- [x] Verify Portal external health and crawler service health three times at ten-second intervals; retain CronJob suspension regardless of result.


## Applied Result — 2026-09-22

| 항목 | 결과 |
|---|---|
| 저장소 반영 | PR #254를 `05a37c7`로 main 병합함. main CI·Trivy·Deploy N100 workflow 성공, workflow는 경로 분류에 따라 실제 앱 배포를 수행하지 않음. |
| N100 동기화 | `05a37c7`로 fast-forward함. 기존 미추적 파일 2개를 보존했으며 추적 파일 변경은 없음. |
| 뉴스 이미지 | 호환 이미지 → 목표 이미지 순으로 image-only 전환함. 목표 rollout 실패가 없어 rollback은 실행하지 않음. |
| SLO 증적 | live CronJob을 `suspend: true`로 전환한 뒤 collector 이미지 필드만 immutable digest로 교체함. ConfigMap 전체 apply·초기화는 수행하지 않음. |
| 수동 검증 | 고유 Job 1회가 Complete됨. 당일 증적의 고정 필드·형식·날짜 중복·최대 보관 수를 검증했고 `overall=ok`임. |
| 자동 실행 | 수동 검증 후 별도 승인으로 활성화함. CronJob은 `suspend: false`임. |
| 외부·서비스 health | Portal 외부 health와 crawler Service health를 10초 간격 3회 확인해 모두 HTTP 200임. |
