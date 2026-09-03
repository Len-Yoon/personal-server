#!/usr/bin/env bash
set -Eeuo pipefail

NAMESPACE="monitoring"
WORKLOAD_NAMESPACES=("monitoring" "personal-server")
RELEASE="personal-server-monitoring"
RELAY="sre-telegram-relay"
PROMETHEUS_SERVICE="personal-server-monitoring-prometheus"
PORT_FORWARD_WAIT_SECONDS="${SRE_TELEGRAM_VERIFY_PORT_FORWARD_WAIT_SECONDS:-60}"
PORT_FORWARD_PID=""
PORT_FORWARD_RESPONSE=""

fail() {
  local stage="$1"
  printf 'sre_telegram_verify_stage=%s\n' "$stage"
  printf 'sre_telegram_verify=FAIL\n'
  return 1
}

cleanup_port_forward() {
  if [ -n "$PORT_FORWARD_PID" ]; then
    kill "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
    wait "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
    PORT_FORWARD_PID=""
  fi
}

wait_for_port_forward_http() {
  local url="$1" deadline response
  PORT_FORWARD_RESPONSE=""
  deadline=$((SECONDS + PORT_FORWARD_WAIT_SECONDS))
  while [ "$SECONDS" -lt "$deadline" ]; do
    kill -0 "$PORT_FORWARD_PID" >/dev/null 2>&1 || return 1
    if response=$(curl --fail --silent --show-error --max-time 2 "$url" 2>/dev/null); then
      PORT_FORWARD_RESPONSE="$response"
      return 0
    fi
    sleep 1
  done
  return 1
}

check_relay_health() {
  sudo k3s kubectl -n "$NAMESPACE" port-forward --address 127.0.0.1 "service/$RELAY" 18080:8080 >/dev/null 2>&1 &
  PORT_FORWARD_PID=$!
  wait_for_port_forward_http http://127.0.0.1:18080/healthz || return 1
  [ "$PORT_FORWARD_RESPONSE" = "ok" ]
}

check_prometheus_targets() {
  sudo k3s kubectl -n "$NAMESPACE" port-forward --address 127.0.0.1 "service/$PROMETHEUS_SERVICE" 19090:9090 >/dev/null 2>&1 &
  PORT_FORWARD_PID=$!
  wait_for_port_forward_http http://127.0.0.1:19090/api/v1/targets || return 1
  printf '%s\n' "$PORT_FORWARD_RESPONSE" | python3 -c 'import json, sys
try:
    payload = json.load(sys.stdin)
    targets = payload.get("data", {}).get("activeTargets")
    healthy = payload.get("status") == "success" and isinstance(targets, list) and bool(targets)
    healthy = healthy and all(isinstance(target, dict) and target.get("health") == "up" for target in targets)
except (TypeError, ValueError):
    healthy = False
raise SystemExit(0 if healthy else 1)'
}

check_relay_service_exposure() {
  local service_json
  service_json=$(sudo k3s kubectl -n "$NAMESPACE" get service "$RELAY" -o json 2>/dev/null) || return 1
  printf '%s\n' "$service_json" | python3 -c 'import json, sys
try:
    spec = json.load(sys.stdin)["spec"]
    ports = spec.get("ports") or []
    exposed = bool(spec.get("externalIPs")) or bool(spec.get("externalName"))
    exposed = exposed or bool(spec.get("loadBalancerIP")) or bool(spec.get("loadBalancerClass"))
    exposed = exposed or bool(spec.get("healthCheckNodePort")) or bool(spec.get("externalTrafficPolicy"))
    exposed = exposed or any("nodePort" in port for port in ports)
    safe = spec.get("type") == "ClusterIP" and bool(ports) and not exposed
except (KeyError, TypeError, ValueError):
    safe = False
raise SystemExit(0 if safe else 1)'
}

check_can_i_denied() {
  local verb="$1" resource="$2" namespace="$3" service_account="$4" result
  result=$(sudo k3s kubectl auth can-i "$verb" "$resource" --namespace "$namespace" --as "$service_account" 2>/dev/null) || :
  [ "$result" = "no" ]
}

check_non_escalated_rbac() {
  local service_account="system:serviceaccount:${NAMESPACE}:${RELAY}"
  local check verb resource namespace
  for namespace in "${WORKLOAD_NAMESPACES[@]}"; do
    for check in \
      'get|secrets' \
      'list|secrets' \
      'watch|secrets' \
      'create|secrets' \
      'delete|secrets' \
      'patch|secrets' \
      'create|pods' \
      'delete|pods' \
      'delete|deployments' \
      'create|pods/exec' \
      'create|pods/portforward' \
      'patch|deployments'; do
      IFS='|' read -r verb resource <<< "$check"
      check_can_i_denied "$verb" "$resource" "$namespace" "$service_account" || return 1
    done
  done
}

main() {
  [ "$#" -eq 0 ] || { printf 'usage: %s\n' "$0" >&2; fail usage; return 2; }
  case "$PORT_FORWARD_WAIT_SECONDS" in
    ''|*[!0-9]*) fail configuration; return 2 ;;
  esac
  [ "$PORT_FORWARD_WAIT_SECONDS" -gt 0 ] || { fail configuration; return 2; }
  trap cleanup_port_forward EXIT
  trap 'exit 130' INT TERM
  sudo k3s kubectl -n "$NAMESPACE" rollout status "deployment/$RELAY" --timeout=120s >/dev/null || { fail relay_rollout; return 1; }
  check_relay_service_exposure || { fail relay_service_exposure; return 1; }
  sudo k3s kubectl -n "$NAMESPACE" get prometheusrule sre-telegram-k3s-alerts >/dev/null || { fail prometheus_rule; return 1; }
  check_non_escalated_rbac || { fail rbac_boundary; return 1; }
  check_relay_health || { fail relay_health; return 1; }
  cleanup_port_forward
  check_prometheus_targets || { fail prometheus_targets; return 1; }
  cleanup_port_forward
  trap - EXIT INT TERM
  printf 'sre_telegram_verify=PASS\n'
}

main "$@"
