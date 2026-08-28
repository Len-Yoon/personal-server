#!/usr/bin/env bash
set -u -o pipefail

# Manual N100 smoke test. This creates no production resource and never edits Caddy.
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-30}"
NODE_PORT=32081
CADDY_CONTAINER="${CADDY_CONTAINER:-personal-server-caddy-1}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
NS=""
RESOURCE_TARGET=0

run_timeout() { timeout "${1}s" "${@:2}"; }
fail() { printf '%s\n' "portal_nodeport_connectivity=FAIL" >&2; return 1; }
valid_run_id() {
  case "$1" in
    ''|*[!a-zA-Z0-9-]*) return 1 ;;
    *) return 0 ;;
  esac
}
configure_target() {
  local run_id_lc
  run_id_lc=$(printf '%s' "$RUN_ID" | tr '[:upper:]' '[:lower:]')
  NS="portal-nodeport-smoke-${run_id_lc}"
}
assert_namespace_absent() {
  ! run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl get namespace "$NS" >/dev/null 2>&1
}
cleanup() {
  local ok=0
  if [ "$RESOURCE_TARGET" -eq 1 ]; then
    run_timeout 120 sudo k3s kubectl delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1 || ok=1
    assert_namespace_absent || ok=1
  fi
  return "$ok"
}
on_signal() {
  cleanup
  printf '%s\n' "portal_nodeport_connectivity=FAIL" >&2
  exit 130
}

main() {
  if [ "${1:-}" = "--cleanup" ]; then
    [ "$#" -eq 2 ] && RUN_ID="$2" || { printf '%s\n' "usage: --cleanup RUN_ID" >&2; fail; return 1; }
    valid_run_id "$RUN_ID" || { printf '%s\n' "invalid RUN_ID" >&2; fail; return 1; }
    configure_target
    RESOURCE_TARGET=1
    if cleanup; then printf '%s\n' "portal_nodeport_connectivity=PASS"; return 0; fi
    fail; return 1
  fi
  [ "$#" -eq 0 ] || { printf '%s\n' "usage: [--cleanup RUN_ID]" >&2; fail; return 1; }
  valid_run_id "$RUN_ID" || { printf '%s\n' "invalid RUN_ID" >&2; fail; return 1; }
  configure_target
  trap on_signal INT TERM HUP
  printf '%s\n' "portal_nodeport_connectivity_run_id=$RUN_ID"

  if run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl get namespace "$NS" >/dev/null 2>&1; then
    printf '%s\n' "RUN_ID collides with an existing namespace" >&2
    fail
    return 1
  fi
  RESOURCE_TARGET=1
  if ! run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Namespace
metadata:
  name: $NS
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: portal-nodeport-smoke-deployment
  namespace: $NS
spec:
  replicas: 1
  selector:
    matchLabels:
      app: portal-nodeport-smoke
  template:
    metadata:
      labels:
        app: portal-nodeport-smoke
    spec:
      automountServiceAccountToken: false
      containers:
        - name: responder
          image: busybox:1.36
          imagePullPolicy: IfNotPresent
          command:
            - sh
            - -c
            - mkdir -p /www; printf '%s\n' portal-nodeport-smoke-ok > /www/health; httpd -f -p 8080 -h /www
          ports:
            - containerPort: 8080
---
apiVersion: v1
kind: Service
metadata:
  name: portal-nodeport-smoke-service
  namespace: $NS
spec:
  type: NodePort
  selector:
    app: portal-nodeport-smoke
  ports:
    - name: http
      port: 8080
      targetPort: 8080
      nodePort: 32081
YAML
  then
    cleanup
    fail
    return 1
  fi
  if ! run_timeout 120 sudo k3s kubectl -n "$NS" rollout status deployment/portal-nodeport-smoke-deployment --timeout=120s >/dev/null; then
    cleanup; fail; return 1
  fi
  if ! run_timeout "$TIMEOUT_SECONDS" docker exec "$CADDY_CONTAINER" curl --fail --silent --show-error --max-time 10 "http://host.docker.internal:${NODE_PORT}/health" | grep -Fqx 'portal-nodeport-smoke-ok'; then
    cleanup; fail; return 1
  fi
  if cleanup; then printf '%s\n' "portal_nodeport_connectivity=PASS"; return 0; fi
  fail; return 1
}
main "$@"
