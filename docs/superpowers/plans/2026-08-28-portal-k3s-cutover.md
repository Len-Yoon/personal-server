# Portal K3s Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare and execute a reversible, Portal-only Compose-to-K3s cutover.

**Architecture:** Docker Caddy keeps TLS and proxies to a validated K3s NodePort through `host.docker.internal`; Portal state is copied once to an ext4 local-path PVC while Compose Portal is stopped.

**Tech Stack:** Docker Compose, Caddy, K3s local-path, Kubernetes Deployment/Service/PVC/Secret, Bash.

**Spec:** `docs/superpowers/specs/2026-08-28-portal-k3s-cutover-design.md`

## Global Constraints

- No Secret value, token, or private key enters Git or terminal output.
- Portal Compose and K3s workloads never share writable data.
- Caddy remains the only owner of host ports 80/443.
- Any failure before public switch restores Compose upstream and stops the K3s Portal writer.

---

### Task 1: Validate Caddy-to-NodePort transport

**Files:** Create `infra/k8s/tools/portal-nodeport-connectivity-smoke.sh`; Test `tests/test_k8s_portal_nodeport_connectivity_smoke.py`.

- [ ] Write a failing test that requires unique temporary resources, a fixed temporary NodePort, Caddy-container curl, and exact cleanup.
- [ ] Run the test and confirm it fails before the script exists.
- [ ] Implement the isolated probe; do not change Caddy routes or application data.
- [ ] Run the test, Bash syntax check, and execute the probe on N100.

### Task 2: Add backwards-compatible Caddy upstream indirection

**Files:** Modify `caddy/Caddyfile`, `docker-compose.n100.yml`; Test `tests/test_caddy_portal_upstream.py`.

- [ ] Add a failing test that Portal hosts use one environment-backed upstream and default remains `portal-web:8000`.
- [ ] Add `PORTAL_UPSTREAM` default and Caddy `host-gateway` mapping without changing the current effective route.
- [ ] Run Caddy validation and relevant tests; deploy only after CI succeeds.

### Task 3: Prepare an operator-only Portal cutover script

**Files:** Create `infra/k8s/tools/portal-cutover.sh`; Test `tests/test_k8s_portal_cutover.py`; Modify `docs/k3s-flux-transition-draft.md`.

- [ ] Test fail-closed preflight: backup gate, encryption status, no concurrent Portal writer, explicit GO argument, and rollback mode.
- [ ] Implement safe PVC copy, digest verification, Secret seed from a 0600 temporary allowlist file, Deployment/Service application, and no public switch by default.
- [ ] Require an explicit final `--switch-caddy` invocation for the traffic change and provide `--rollback-caddy`.
- [ ] Execute only after a final maintenance-window GO approval.
