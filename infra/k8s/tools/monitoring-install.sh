#!/usr/bin/env bash
set -Eeuo pipefail

RELEASE="personal-server-monitoring"
CHART="prometheus-community/kube-prometheus-stack"
NAMESPACE="monitoring"
VERSION="88.6.1"
VALUES="infra/k8s/monitoring/values.n100.yaml"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PREFLIGHT_SCRIPT="${MONITORING_PREFLIGHT_SCRIPT:-$SCRIPT_DIR/monitoring-preflight.sh}"

fail() {
  printf '%s\n' "monitoring_install=FAIL" >&2
  return 1
}

usage() {
  printf '%s\n' "usage: $0 --render | --apply (explicit --apply is required for installation)" >&2
  printf '%s\n' "monitoring_install=FAIL" >&2
}

main() {
  local mode="${1:-}"
  [ "$#" -eq 1 ] || { usage; return 2; }
  case "$mode" in
    --render)
      if helm template "$RELEASE" "$CHART" --namespace "$NAMESPACE" --version "$VERSION" --values "$VALUES"; then
        printf '%s\n' "monitoring_install=PASS"
        return 0
      fi
      fail
      return 1
      ;;
    --apply)
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
        --values "$VALUES" --wait --timeout 10m; then
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
    *)
      usage
      return 2
      ;;
  esac
}

main "$@"
