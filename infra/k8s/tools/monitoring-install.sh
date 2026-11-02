#!/usr/bin/env bash
set -Eeuo pipefail

RELEASE="personal-server-monitoring"
CHART="prometheus-community/kube-prometheus-stack"
NAMESPACE="monitoring"
VERSION="88.6.1"
VALUES="infra/k8s/monitoring/values.n100.yaml"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PREFLIGHT_SCRIPT="${MONITORING_PREFLIGHT_SCRIPT:-$SCRIPT_DIR/monitoring-preflight.sh}"
POST_RENDERER="$SCRIPT_DIR/monitoring-dashboard-post-renderer.py"

renderer_ready() {
  [ -x "$POST_RENDERER" ] && python3 -c 'import yaml' >/dev/null 2>&1
}

existing_release_ready() {
  local status listing
  status=$(helm status "$RELEASE" --namespace "$NAMESPACE" --output json 2>/dev/null) || return 1
  printf '%s' "$status" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if isinstance(d,dict) and d.get("name")=="personal-server-monitoring" and d.get("namespace")=="monitoring" and d.get("info",{}).get("status")=="deployed" else 1)' >/dev/null 2>&1 || return 1
  listing=$(helm list --namespace "$NAMESPACE" --all --filter '^personal-server-monitoring$' --output json 2>/dev/null) || return 1
  printf '%s' "$listing" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if isinstance(d,list) and len(d)==1 and isinstance(d[0],dict) and all(d[0].get(k)==v for k,v in {"name":"personal-server-monitoring","namespace":"monitoring","status":"deployed","chart":"kube-prometheus-stack-88.6.1"}.items()) else 1)' >/dev/null 2>&1
}

fail() {
  printf '%s\n' "monitoring_install=FAIL" >&2
  return 1
}

usage() {
  printf '%s\n' "usage: $0 --render | --render-upgrade | --apply | --upgrade (writes require explicit mode and operational approval)" >&2
  printf '%s\n' "monitoring_install=FAIL" >&2
}

main() {
  local mode="${1:-}"
  [ "$#" -eq 1 ] || { usage; return 2; }
  case "$mode" in
    --render|--render-upgrade)
      renderer_ready || { fail; return 1; }
      if [ "$mode" = --render-upgrade ]; then set -- --is-upgrade; else set --; fi
      if helm template "$RELEASE" "$CHART" --namespace "$NAMESPACE" --version "$VERSION" --values "$VALUES" --post-renderer "$POST_RENDERER" "$@"; then
        printf '%s\n' "monitoring_install=PASS" >&2
        return 0
      fi
      fail
      return 1
      ;;
    --apply)
      renderer_ready || { fail; return 1; }
      local preflight_output namespace_lookup namespace_created=0
      if ! preflight_output=$("$PREFLIGHT_SCRIPT" 2>&1) || ! grep -Fqx 'monitoring_preflight=PASS' <<< "$preflight_output"; then
        fail
        return 1
      fi
      if namespace_lookup=$(sudo k3s kubectl get namespace "$NAMESPACE" 2>&1); then
        printf '%s\n' "monitoring namespace already exists; refusing to apply" >&2
        fail
        return 1
      elif ! grep -Fq "Error from server (NotFound): namespaces \"$NAMESPACE\" not found" <<< "$namespace_lookup"; then
        printf '%s\n' "unable to establish monitoring namespace absence" >&2
        fail
        return 1
      fi
      if ! sudo k3s kubectl create namespace "$NAMESPACE" >/dev/null; then
        fail
        return 1
      fi
      namespace_created=1
      if helm upgrade --install "$RELEASE" "$CHART" \
        --namespace "$NAMESPACE" --version "$VERSION" \
        --values "$VALUES" --wait --timeout 10m --post-renderer "$POST_RENDERER"; then
        printf '%s\n' "monitoring_install=PASS"
        return 0
      fi
      helm uninstall "$RELEASE" --namespace "$NAMESPACE" --wait --timeout 5m >/dev/null 2>&1 || true
      if [ "$namespace_created" -eq 1 ]; then
        sudo k3s kubectl delete namespace "$NAMESPACE" --ignore-not-found --wait=true >/dev/null 2>&1 || true
      fi
      fail
      return 1
      ;;
    --upgrade)
      renderer_ready || { fail; return 1; }
      local upgrade_preflight
      if ! upgrade_preflight=$("$PREFLIGHT_SCRIPT" 2>&1) || ! grep -Fqx 'monitoring_preflight=PASS' <<< "$upgrade_preflight" || ! existing_release_ready; then
        fail
        return 1
      fi
      # A historical Helm rollback may predate the manual dashboard corrections.
      # Never uninstall/delete or auto-rollback an existing monitoring release.
      if helm upgrade "$RELEASE" "$CHART" --namespace "$NAMESPACE" --version "$VERSION" \
        --reuse-values --wait --timeout 10m --post-renderer "$POST_RENDERER"; then
        printf '%s\n' "monitoring_install=PASS"
        return 0
      fi
      fail
      return 1
      ;;
    *)
      usage
      return 2
      ;;
  esac
}

main "$@"
