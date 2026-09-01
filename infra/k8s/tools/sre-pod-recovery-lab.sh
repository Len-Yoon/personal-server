#!/usr/bin/env bash
set -Eeuo pipefail
usage(){ echo "사용법: $0 --run | --cleanup <run-id>" >&2; }
die(){ echo "sre_pod_recovery=FAIL"; echo "${1:-실패}" >&2; exit 1; }
namespace_state(){
  local ns="$1" output
  if output="$(sudo k3s kubectl get namespace "$ns" 2>&1)"; then return 0; fi
  printf '%s\n' "$output" >&2
  printf '%s\n' "$output" | grep -Eqi 'notfound|not found' && return 1
  return 2
}
cleanup_run(){
  local run_id="$1" run_id_lc ns; run_id_lc="$(printf '%s' "$run_id" | tr '[:upper:]' '[:lower:]')"
  [[ "$run_id_lc" =~ ^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$ ]] || die "유효하지 않은 run id"
  ns="sre-recovery-lab-${run_id_lc}"
  sudo k3s kubectl delete namespace "$ns" --ignore-not-found=true
  local state; if namespace_state "$ns"; then state=0; else state=$?; fi
  case "$state" in 1) ;; 0) die "namespace 정리 확인 실패";; *) die "namespace 상태 확인 실패";; esac
}
run_lab(){
  local run_id run_id_lc NS POD_LABEL baseline after deadline pod
  run_id="${SRE_RECOVERY_LAB_RUN_ID:-$(date -u +%Y%m%d%H%M%S)-$$}"; run_id_lc="$(printf '%s' "$run_id" | tr '[:upper:]' '[:lower:]')"
  [[ "$run_id_lc" =~ ^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$ ]] || die "유효하지 않은 run id"
  NS="sre-recovery-lab-${run_id_lc}"; POD_LABEL='app.kubernetes.io/name=sre-pod-recovery'
  trap 'rc=$?; cleanup_run "$run_id" >/dev/null 2>&1 || true; trap - EXIT INT TERM; exit "$rc"' EXIT INT TERM
  local state; if namespace_state "$NS"; then state=0; else state=$?; fi
  case "$state" in 0) die "namespace already exists";; 1) ;; *) die "namespace 상태 확인 실패";; esac
  sudo k3s kubectl apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${NS}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: sre-pod-recovery
  namespace: ${NS}
spec:
  replicas: 1
  selector:
    matchLabels: {app.kubernetes.io/name: sre-pod-recovery}
  template:
    metadata: {labels: {app.kubernetes.io/name: sre-pod-recovery}}
    spec:
      containers:
      - name: recovery
        image: busybox:1.36
        command: ["sh", "-c", "while true; do sleep 3600; done"]
        livenessProbe:
          exec: {command: ["sh", "-c", "test ! -f /tmp/force-liveness-failure"]}
          initialDelaySeconds: 2
          periodSeconds: 2
        readinessProbe:
          exec: {command: ["sh", "-c", "test ! -f /tmp/force-liveness-failure"]}
          initialDelaySeconds: 1
          periodSeconds: 2
EOF
  sudo k3s kubectl -n "$NS" wait --for=condition=Available deployment/sre-pod-recovery --timeout="${SRE_RECOVERY_LAB_TIMEOUT:-90}s"
  pod="$(sudo k3s kubectl -n "$NS" get pod -l "$POD_LABEL" -o jsonpath='{.items[0].metadata.name}')"
  baseline="$(sudo k3s kubectl -n "$NS" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
  sudo k3s kubectl -n "$NS" exec "$pod" -- touch /tmp/force-liveness-failure
  deadline=$((SECONDS + ${SRE_RECOVERY_LAB_TIMEOUT:-90})); after="$baseline"
  while (( SECONDS < deadline )); do
    pod="$(sudo k3s kubectl -n "$NS" get pod -l "$POD_LABEL" -o jsonpath='{.items[0].metadata.name}')" || true
    after="$(sudo k3s kubectl -n "$NS" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null || echo 0)"
    if (( after > baseline )) && sudo k3s kubectl -n "$NS" wait --for=condition=Ready pod "$pod" --timeout=5s >/dev/null 2>&1; then
      echo "sre_pod_recovery=PASS"; echo "sre_pod_recovery_run_id=${run_id_lc}"; return 0
    fi
    sleep 2
  done
  die "restartCount 증가 또는 Ready 복구 확인 실패 (baseline=${baseline}, after=${after})"
}
case "${1:-}" in
  --run) [[ $# -eq 1 ]] || { usage; exit 2; }; run_lab;;
  --cleanup) [[ $# -eq 2 ]] || { usage; exit 2; }; cleanup_run "$2"; echo "sre_pod_recovery=PASS"; echo "sre_pod_recovery_run_id=$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')";;
  *) usage; exit 2;;
esac
