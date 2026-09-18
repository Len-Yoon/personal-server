# Telegram Relay Probe Separation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telegram API 일시 장애가 relay Pod liveness 재시작으로 증폭되지 않도록 readiness와 liveness probe를 분리함.

**Architecture:** readiness는 기존 `/healthz` HTTP 계약을 유지해 Telegram 전달 가능 상태를 노출함. liveness는 `http` TCP 포트 수신 여부만 확인해 외부 의존성 실패를 프로세스 사망으로 해석하지 않음.

**Tech Stack:** Kubernetes Deployment YAML, Python unittest, K3s

**Spec:** `docs/superpowers/specs/2026-09-18-relay-postboot-observability-design.md`

## Global Constraints

- `sre-telegram-relay`는 단일 replica·`Recreate` 전략을 유지함.
- Secret·RBAC·Service·이미지·애플리케이션 코드·resource requests/limits는 변경하지 않음.
- Portal·PVC·Caddy·Cloudflare Tunnel ingress는 변경하지 않음.
- 실제 N100 적용 전 `lastState`, Event, 이전 로그를 읽기 전용으로 확인함.
- 적용 실패 시 기존 HTTP `/healthz` liveness 매니페스트만 원복함.

---

### Task 1: Relay probe 계약 갱신

**Files:**
- Modify: `tests/test_k8s_sre_telegram_manifests.py`
- Modify: `infra/k8s/sre-telegram/base.yaml`

**Interfaces:**
- Consumes: Deployment container port 이름 `http`, readiness endpoint `/healthz`
- Produces: readiness HTTP + liveness TCP probe 계약

- [ ] **Step 1: Write the failing test**

`tests/test_k8s_sre_telegram_manifests.py`의 relay Deployment 검증에 아래 계약을 추가함.

```python
container = deployment["spec"]["template"]["spec"]["containers"][0]
self.assertEqual(container["readinessProbe"]["httpGet"], {"path": "/healthz", "port": "http"})
self.assertEqual(container["livenessProbe"]["tcpSocket"], {"port": "http"})
self.assertNotIn("httpGet", container["livenessProbe"])
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_k8s_sre_telegram_manifests
```

Expected: FAIL because the current liveness probe still contains `httpGet`.

- [ ] **Step 3: Write the minimal implementation**

In `infra/k8s/sre-telegram/base.yaml`, replace only the liveness probe with:

```yaml
livenessProbe:
  tcpSocket:
    port: http
```

Keep `readinessProbe.httpGet.path: /healthz` and `port: http` unchanged.

- [ ] **Step 4: Run focused regression tests**

Run:

```bash
python3 -m unittest tests.test_k8s_sre_telegram_manifests tests.test_sre_telegram_relay
git diff --check
```

Expected: all tests pass; no whitespace error.

- [ ] **Step 5: Commit the relay change**

```bash
git add infra/k8s/sre-telegram/base.yaml tests/test_k8s_sre_telegram_manifests.py
git commit -m "fix: Telegram relay liveness probe 분리"
```

### Task 2: N100 relay preflight and apply verification

**Files:**
- No repository file changes

**Interfaces:**
- Consumes: committed relay manifest and existing local relay image
- Produces: verified K3s Deployment rollout without Secret inspection

- [ ] **Step 1: Capture pre-apply evidence**

Run read-only checks for relay Pod `restartCount`, previous `lastState.terminated.reason`, exit code, QoS, recent Events, and previous container logs. Do not print Secret data or internal addresses.

- [ ] **Step 2: Confirm exact manifest diff**

Run `kubectl diff -f infra/k8s/sre-telegram/base.yaml` and proceed only when the relay Deployment probe field is the intended difference. Stop on unrelated resources.

- [ ] **Step 3: Apply and wait for readiness**

Apply only `infra/k8s/sre-telegram/base.yaml`, then wait for `deployment/sre-telegram-relay` rollout completion. Do not restart unrelated workloads.

- [ ] **Step 4: Verify operational result**

Confirm relay Pod Ready, no new `Liveness probe failed` Event, TCP liveness configuration, `/healthz` readiness response, Prometheus `up` state, and public health three times at 10-second intervals.

- [ ] **Step 5: Roll back only on verified relay regression**

If rollout or readiness fails, reapply the captured pre-apply Deployment definition and verify recovery. Do not modify Secret, ConfigMap state, Portal, PVC, Caddy, or Tunnel.
