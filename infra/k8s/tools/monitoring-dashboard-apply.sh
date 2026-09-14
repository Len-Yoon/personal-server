#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
DASHBOARD="$(CDPATH= cd -- "$SCRIPT_DIR/../monitoring" && pwd)/portal-http-dashboard.yaml"

fail() {
  printf '%s\n' "$1" >&2
  printf 'monitoring_dashboard=FAIL\n'
  exit 1
}

normalized_manifest() {
  tr -d '\r' < "$DASHBOARD"
}

count_manifest_line() {
  normalized_manifest | grep -Fxc "$1"
}

verify_dashboard_contract() {
  [ -f "$DASHBOARD" ] || fail "dashboard manifest is missing: $DASHBOARD"
  ! normalized_manifest | grep -Eq '^(---|\.\.\.)([[:space:]]*(#.*)?)$' || fail "dashboard manifest must contain one document"
  [ "$(count_manifest_line 'apiVersion: v1')" -eq 1 ] || fail "dashboard apiVersion contract is invalid"
  [ "$(count_manifest_line 'kind: ConfigMap')" -eq 1 ] || fail "dashboard kind contract is invalid"
  [ "$(count_manifest_line '  name: portal-http-observability')" -eq 1 ] || fail "dashboard name contract is invalid"
  [ "$(count_manifest_line '  namespace: monitoring')" -eq 1 ] || fail "dashboard namespace contract is invalid"
  [ "$(count_manifest_line '    grafana_dashboard: "1"')" -eq 1 ] || fail "dashboard label contract is invalid"
  [ "$(normalized_manifest | grep -Fc '  portal-http-observability.json:')" -eq 1 ] || fail "dashboard data contract is invalid"
  sudo k3s kubectl -n monitoring get deployment personal-server-monitoring-grafana >/dev/null \
    || fail "Grafana Deployment is unavailable"
}

mode="${1:-}"
case "$mode" in
  --check)
    verify_dashboard_contract
    ;;
  --apply)
    verify_dashboard_contract
    sudo k3s kubectl apply --dry-run=server -f "$DASHBOARD" || fail "dashboard server dry-run failed"
    sudo k3s kubectl apply -f "$DASHBOARD" || fail "dashboard apply failed"
    ;;
  --rollback)
    sudo k3s kubectl -n monitoring delete configmap portal-http-observability --ignore-not-found \
      || fail "dashboard rollback failed"
    ;;
  *)
    printf 'usage: %s --check|--apply|--rollback\n' "$0" >&2
    exit 2
    ;;
esac

printf 'monitoring_dashboard=PASS\n'
