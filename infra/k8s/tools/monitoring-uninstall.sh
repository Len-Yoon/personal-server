#!/usr/bin/env bash
set -Eeuo pipefail

NAMESPACE="monitoring"
RELEASE="personal-server-monitoring"

fail() {
  printf '%s\n' "monitoring_uninstall=FAIL" >&2
  return 1
}

main() {
  local delete_data=0
  if [ "$#" -lt 1 ] || [ "$1" != "--uninstall" ] || [ "$#" -gt 2 ]; then
    printf '%s\n' "usage: $0 --uninstall [--delete-data] (explicit --uninstall is required)" >&2
    printf '%s\n' "monitoring_uninstall=FAIL" >&2
    return 2
  fi
  if [ "$#" -eq 2 ]; then
    [ "$2" = "--delete-data" ] || { printf '%s\n' "usage: $0 --uninstall [--delete-data]" >&2; printf '%s\n' "monitoring_uninstall=FAIL" >&2; return 2; }
    delete_data=1
  fi
  if ! helm uninstall "$RELEASE" --namespace "$NAMESPACE" --wait --timeout 5m; then
    fail
    return 1
  fi
  if [ "$delete_data" -eq 1 ]; then
    if ! sudo k3s kubectl -n "$NAMESPACE" delete pvc --all --ignore-not-found; then
      fail
      return 1
    fi
    if ! sudo k3s kubectl delete namespace "$NAMESPACE" --ignore-not-found --wait=true; then
      fail
      return 1
    fi
  fi
  printf '%s\n' "monitoring_uninstall=PASS"
}

main "$@"
