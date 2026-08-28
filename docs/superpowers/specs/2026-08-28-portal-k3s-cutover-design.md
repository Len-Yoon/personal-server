# Portal K3s Cutover Design

## Goal

Move only `portal-web` from Docker Compose to K3s while Docker Caddy retains TLS and all public hostnames.

## Boundaries

- In scope: `len.pe.kr`, `portfolio.len.pe.kr`, `file.len.pe.kr`, and `admin.len.pe.kr`; one Portal workload; its file data; its runtime Secret.
- Out of scope: Caddy 80/443 ownership, Flux, crawler, YouTube memo, book memo, HomeOps, system-agent, car-care, and all schedulers.
- No secret value is committed or printed. K3s Secret encryption must remain Enabled.

## Architecture

Docker Caddy continues to own public TLS and forwards the four Portal hosts to a fixed K3s NodePort through `host.docker.internal`. Caddy receives an explicit `host-gateway` mapping and a `PORTAL_UPSTREAM` environment value that defaults to the current `portal-web:8000`; deploying this compatibility change does not move traffic.

K3s runs one `portal-web` Deployment with a dynamic `local-path` RWO PVC, an immutable runtime Secret seeded locally, default-deny NetworkPolicy, `automountServiceAccountToken: false`, and an explicit Uvicorn command. No Ingress, LoadBalancer, hostPath, or public NodePort route is used.

## Cutover sequence

1. Validate Caddy-container-to-temporary-NodePort connectivity without changing public traffic.
2. Deploy the backwards-compatible Caddy upstream indirection while it still targets Compose.
3. At the approved maintenance window: verify encrypted backup, stop only Compose Portal, copy `data/files` to the new PVC, verify manifest digest, create runtime Secret, and start K3s Portal.
4. Validate Pod health and Caddy-container NodePort health; change only `PORTAL_UPSTREAM`; recreate only Caddy; validate all four public hosts.
5. Roll back by restoring `PORTAL_UPSTREAM=portal-web:8000`, recreating only Caddy, and keeping the K3s Deployment stopped until data consistency is assessed.

## Go/No-Go gates

- Go: backup restoration evidence is current; temp NodePort is reachable from Docker Caddy; PVC digest matches the stopped Compose source; Pod and all public hosts are healthy.
- No-Go: any gate fails. Restore the Caddy upstream before restarting Compose Portal. Never run Compose and K3s Portal against the same writable data.
