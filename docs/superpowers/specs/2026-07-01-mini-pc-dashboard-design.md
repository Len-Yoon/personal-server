# Mini PC Dashboard Design

## Goal

Add a practical N100 Windows mini PC operations dashboard to the personal server while keeping the project safe to show publicly.

## Scope

The first version adds a lightweight `system-agent` service, a Windows host metrics collector script, portal dashboard summaries, demo-mode masking, global search entry points, and README/operator documentation updates.

## Architecture

`system-agent` is a small FastAPI service that exposes `/health` and `/metrics`. It reads Docker/container status from a conservative local model, host metrics from `data/system/host-metrics.json`, backup status from `data/backups`, and file usage from `data/files`. It never needs write access outside the shared `data/` tree.

Windows host metrics come from a PowerShell script that writes JSON. This avoids pretending a Linux container can fully observe the Windows host. If the JSON is missing or stale, the agent returns a warning while keeping the portal usable.

`portal-web` calls the agent through an internal URL and renders a compact operations overview. If `DEMO_MODE=true`, portal output uses sample metrics and masks local paths or sensitive events.

## Components

- `system-agent`: owns host/backup/container metrics APIs.
- `scripts/windows-host-metrics.ps1`: writes Windows CPU, memory, disk, and uptime data to JSON.
- `portal-web/app/services/system_status.py`: fetches agent metrics, applies demo fallback, and normalizes failures.
- `portal-web/app/services/global_search.py`: aggregates search results from local service APIs.
- Existing memo/news services: expose small JSON search endpoints.
- README and N100 docs: describe the new services, environment variables, and Windows scheduled task setup.

## Data Flow

1. Windows Task Scheduler runs `scripts/windows-host-metrics.ps1`.
2. The script writes `data/system/host-metrics.json`.
3. `system-agent` reads that JSON plus mounted data directories and returns normalized metrics.
4. `portal-web` fetches the agent metrics for the dashboard.
5. Global search calls service search endpoints and renders grouped results.

## Error Handling

Agent failures must not break the portal. `portal-web` shows an unavailable status with a clear warning. Stale Windows metrics are reported as `host_metrics_stale`. Missing backups are reported as `backup_missing`. Demo mode always returns safe sample data.

## Testing

Use Python `unittest`. Add tests for system-agent metric normalization, portal fallback/demo behavior, and search API behavior in memo services. Existing portal security tests remain part of the verification command.

## Non-Goals

This version does not add Prometheus, Grafana, Telegram alerts, or a native Windows background service. It leaves those as later extensions after the lightweight dashboard is useful.
