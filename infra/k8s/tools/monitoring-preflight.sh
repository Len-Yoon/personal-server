#!/usr/bin/env bash
set -u

if [ "$#" -ne 0 ]; then
  printf 'usage: monitoring-preflight.sh\n' >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
VALUES_FILE="$REPO_ROOT/infra/k8s/monitoring/values.n100.yaml"
overall=0

if [ ! -r "$VALUES_FILE" ]; then
  overall=1
fi

nodes=""
if ! nodes="$(sudo k3s kubectl get nodes --no-headers 2>/dev/null)"; then
  overall=1
elif ! printf '%s\n' "$nodes" | awk 'NF < 2 || $2 !~ /^Ready(,SchedulingDisabled)?$/ { bad=1 } END { exit(NR > 0 && !bad ? 0 : 1) }'; then
  overall=1
fi

if ! sudo k3s kubectl get storageclass local-path >/dev/null 2>&1; then
  overall=1
fi

helm_version=""
if ! helm_version="$(helm version --short 2>/dev/null)"; then
  overall=1
elif [[ "$helm_version" != v3.* ]]; then
  overall=1
fi

chart_metadata=""
if ! chart_metadata="$(helm show chart prometheus-community/kube-prometheus-stack --version 88.6.1 2>/dev/null)"; then
  overall=1
elif ! printf '%s\n' "$chart_metadata" | awk -F ': *' '$1 == "version" && $2 == "88.6.1" { found=1 } END { exit(found ? 0 : 1) }'; then
  overall=1
fi

disk_usage=""
if ! disk_usage="$(df -Pk /var/lib/rancher/k3s/storage 2>/dev/null)"; then
  overall=1
elif ! printf '%s\n' "$disk_usage" | awk 'NR > 1 && $4 ~ /^[0-9]+$/ && ($4 + 0) >= 8388608 { found=1 } END { exit(found ? 0 : 1) }'; then
  overall=1
fi

if [ "$overall" -eq 0 ]; then
  printf 'chart_version=88.6.1\n'
  printf 'monitoring_preflight=PASS\n'
  exit 0
fi

printf 'monitoring_preflight=FAIL\n'
exit 1
