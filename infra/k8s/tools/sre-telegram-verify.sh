#!/usr/bin/env bash
set -u

NAMESPACE="monitoring"
RELEASE="personal-server-monitoring"
RELAY="sre-telegram-relay"
PROMETHEUS_SERVICE="personal-server-monitoring-kube-prometheus-prometheus"
PORT_FORWARD_PID=""

fail() {
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

check_relay_health() {
  local response
  sudo k3s kubectl -n "$NAMESPACE" port-forward --address 127.0.0.1 "service/$RELAY" 18080:8080 >/dev/null 2>&1 &
  PORT_FORWARD_PID=$!
  sleep 1
  kill -0 "$PORT_FORWARD_PID" >/dev/null 2>&1 || return 1
  response=$(curl --fail --silent --show-error --max-time 10 http://127.0.0.1:18080/healthz 2>/dev/null) || return 1
  [ "$response" = "ok" ]
}

check_prometheus_targets() {
  local response
  sudo k3s kubectl -n "$NAMESPACE" port-forward --address 127.0.0.1 "service/$PROMETHEUS_SERVICE" 19090:9090 >/dev/null 2>&1 &
  PORT_FORWARD_PID=$!
  sleep 1
  kill -0 "$PORT_FORWARD_PID" >/dev/null 2>&1 || return 1
  response=$(curl --fail --silent --show-error --max-time 10 http://127.0.0.1:19090/api/v1/targets 2>/dev/null) || return 1
  grep -Fq '"health":"up"' <<< "$response"
}

check_non_escalated_rbac() {
  local service_account="system:serviceaccount:${NAMESPACE}:${RELAY}"
  ! sudo k3s kubectl auth can-i get secrets --namespace "$NAMESPACE" --as "$service_account" | grep -Fxq yes || return 1
  ! sudo k3s kubectl auth can-i delete pods --namespace "$NAMESPACE" --as "$service_account" | grep -Fxq yes || return 1
  ! sudo k3s kubectl auth can-i create pods/exec --namespace "$NAMESPACE" --as "$service_account" | grep -Fxq yes || return 1
  ! sudo k3s kubectl auth can-i patch deployments --namespace "$NAMESPACE" --as "$service_account" | grep -Fxq yes
}

main() {
  [ "$#" -eq 0 ] || { printf 'usage: %s\n' "$0" >&2; fail; return 2; }
  trap cleanup_port_forward EXIT
  sudo k3s kubectl -n "$NAMESPACE" rollout status "deployment/$RELAY" --timeout=120s >/dev/null || { fail; return 1; }
  [ "$(sudo k3s kubectl -n "$NAMESPACE" get service "$RELAY" -o jsonpath='{.spec.type}')" = "ClusterIP" ] || { fail; return 1; }
  sudo k3s kubectl -n "$NAMESPACE" get prometheusrule sre-telegram-k3s-alerts >/dev/null || { fail; return 1; }
  check_non_escalated_rbac || { fail; return 1; }
  check_relay_health || { fail; return 1; }
  cleanup_port_forward
  check_prometheus_targets || { fail; return 1; }
  cleanup_port_forward
  trap - EXIT
  printf 'sre_telegram_verify=PASS\n'
}

main "$@"
