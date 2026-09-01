#!/usr/bin/env bash
set -Eeuo pipefail

RELEASE="personal-server-monitoring"
CHART="prometheus-community/kube-prometheus-stack"
NAMESPACE="monitoring"
VERSION="88.6.1"
VALUES="infra/k8s/monitoring/values.n100.yaml"

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
      if helm upgrade --install "$RELEASE" "$CHART" \
        --namespace "$NAMESPACE" --create-namespace --version "$VERSION" \
        --values "$VALUES" --wait --timeout 10m; then
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
