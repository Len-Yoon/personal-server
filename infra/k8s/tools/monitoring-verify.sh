#!/usr/bin/env bash
set -Eeuo pipefail

NAMESPACE="monitoring"
GRAFANA_SERVICE="personal-server-monitoring-grafana"
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
  local pvc_status service_type
  pvc_status=$(sudo k3s kubectl -n "$NAMESPACE" get pvc -o jsonpath='{range .items[*]}{.status.phase}{"\n"}{end}') || return 1
  if [ -n "$pvc_status" ] && printf '%s\n' "$pvc_status" | grep -Fvqx 'Bound'; then
    return 1
  fi
  sudo k3s kubectl -n "$NAMESPACE" get pods >/dev/null || return 1
  sudo k3s kubectl -n "$NAMESPACE" wait --for=condition=Ready pod --all --timeout=120s >/dev/null || return 1
  service_type=$(sudo k3s kubectl -n "$NAMESPACE" get service "$GRAFANA_SERVICE" -o jsonpath='{.spec.type}') || return 1
  [ "$service_type" = "ClusterIP" ]
}

port_forward_check() {
  local check_status=0
  trap cleanup_port_forward EXIT
  sudo k3s kubectl -n "$NAMESPACE" port-forward --address 127.0.0.1 "service/$GRAFANA_SERVICE" 3000:80 >/dev/null 2>&1 &
  PORT_FORWARD_PID=$!
  sleep 1
  curl --fail --silent --show-error --max-time 10 http://127.0.0.1:3000/login >/dev/null || check_status=$?
  cleanup_port_forward
  PORT_FORWARD_PID=""
  trap - EXIT
  return "$check_status"
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
