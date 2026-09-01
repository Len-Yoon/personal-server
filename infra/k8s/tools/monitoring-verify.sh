#!/usr/bin/env bash
set -Eeuo pipefail

NAMESPACE="monitoring"
GRAFANA_SERVICE="personal-server-monitoring-grafana"
PROMETHEUS_SERVICE="personal-server-monitoring-prometheus"
PORT_FORWARD_PID=""

fail() {
  printf '%s\n' "monitoring_verify=FAIL" >&2
  return 1
}

cleanup_port_forward() {
  if [ -n "$PORT_FORWARD_PID" ]; then
    kill "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
    wait "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
  fi
}

verify_resources() {
  local pvc_status service_type dashboard
  pvc_status=$(sudo k3s kubectl -n "$NAMESPACE" get pvc --no-headers) || return 1
  [ "$(printf '%s\n' "$pvc_status" | awk 'NF {count++} END {print count+0}')" -eq 2 ] || return 1
  printf '%s\n' "$pvc_status" | awk 'NF && $NF != "Bound" {bad=1} END {exit bad}' || return 1
  sudo k3s kubectl -n "$NAMESPACE" get pods >/dev/null || return 1
  sudo k3s kubectl -n "$NAMESPACE" wait --for=condition=Ready pod --all --timeout=120s >/dev/null || return 1
  service_type=$(sudo k3s kubectl -n "$NAMESPACE" get service "$GRAFANA_SERVICE" -o jsonpath='{.spec.type}') || return 1
  [ "$service_type" = "ClusterIP" ] || return 1
  dashboard=$(sudo k3s kubectl -n "$NAMESPACE" get configmap -l grafana_dashboard=1 -o name) || return 1
  [ -n "$dashboard" ]
}

run_local_check() {
  local service="$1" local_port="$2" url="$3" require_target="$4" check_status=0 response
  trap cleanup_port_forward EXIT
  sudo k3s kubectl -n "$NAMESPACE" port-forward --address 127.0.0.1 "service/$service" "$local_port" >/dev/null 2>&1 &
  PORT_FORWARD_PID=$!
  sleep 1
  if ! kill -0 "$PORT_FORWARD_PID" >/dev/null 2>&1; then
    cleanup_port_forward
    PORT_FORWARD_PID=""
    trap - EXIT
    return 1
  fi
  response=$(curl --fail --silent --show-error --max-time 10 "$url" 2>/dev/null) || check_status=$?
  if [ "$check_status" -eq 0 ] && [ "$require_target" -eq 1 ]; then
    grep -Fq '"health":"up"' <<< "$response" || check_status=1
  fi
  cleanup_port_forward
  PORT_FORWARD_PID=""
  trap - EXIT
  return "$check_status"
}

port_forward_check() {
  run_local_check "$GRAFANA_SERVICE" "3000:80" http://127.0.0.1:3000/login 0 || return 1
  run_local_check "$PROMETHEUS_SERVICE" "9090:9090" http://127.0.0.1:9090/api/v1/targets 1
}

main() {
  local port_check=0
  if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "${1:-}" != "--port-forward-check" ]; }; then
    printf '%s\n' "usage: $0 [--port-forward-check]" >&2
    fail
    return 2
  fi
  [ "$#" -eq 1 ] && port_check=1
  if ! verify_resources; then
    fail
    return 1
  fi
  if [ "$port_check" -eq 1 ] && ! port_forward_check; then
    fail
    return 1
  fi
  printf '%s\n' "monitoring_verify=PASS"
}

main "$@"
