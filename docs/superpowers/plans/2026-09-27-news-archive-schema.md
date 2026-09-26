# News archive safety implementation plan

**Goal:** Preserve notification state while making crawler archive loading and legacy migration fail closed.

**Scope:** Crawler archive storage, focused tests, and operational documentation. Keep schema v3 and single-writer ownership. No live data or deployment changes. The host maintenance pruner is blocked by repository policy and remains a deployment risk.

## Tasks

1. Add regression tests for corrupt/unknown schema, v2 migration backup and first-collection behavior. Confirm they fail on the current code.
2. Make the crawler refuse unknown/corrupt archives; migrate only known v2 after verified original backup, dropping v2 articles and resetting notification initialization to avoid a first-refresh alert burst.
3. Preserve the existing crawler read-modify-write lock and atomic replacement; explicitly document the unresolved host maintenance writer and deployment preflight needed.
4. Run focused and required repository tests, change harness, diff review, and independent specialist review. Record rollback limits and live-state verification required before deployment.
