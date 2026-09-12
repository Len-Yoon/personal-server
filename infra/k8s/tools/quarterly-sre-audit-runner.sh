#!/usr/bin/env bash
set -Eeuo pipefail

STATUS_NAMESPACE=monitoring
STATUS_CONFIGMAP=sre-telegram-quarterly-audit-status
PORTAL_NAMESPACE=personal-server
RECOVERY_NAMESPACE=sre-recovery-lab
RECOVERY_DEPLOYMENT=quarterly-sre-recovery-drill
RECOVERY_LABEL=app.kubernetes.io/name=quarterly-sre-recovery-drill
BACKUP_MAX_AGE_SECONDS=${QUARTERLY_SRE_AUDIT_BACKUP_MAX_AGE_SECONDS:-86400}

health_check=failed
backup_evidence=failed
recovery_lab=failed
recovery_deployment_created=false

configure_client() {
  : "${KUBERNETES_SERVICE_HOST:?Kubernetes API host is required}"
  : "${KUBERNETES_SERVICE_PORT_HTTPS:?Kubernetes API port is required}"
  export KUBECONFIG=/tmp/kubeconfig
  cat >"$KUBECONFIG" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: in-cluster
    cluster:
      certificate-authority: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
      server: https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT_HTTPS}
users:
  - name: runner
    user:
      tokenFile: /var/run/secrets/kubernetes.io/serviceaccount/token
contexts:
  - name: in-cluster
    context:
      cluster: in-cluster
      user: runner
current-context: in-cluster
EOF
  chmod 0600 "$KUBECONFIG"
}

report_status() {
  local overall=failed completed_at run_id payload
  [[ "$health_check" == passed && "$backup_evidence" == passed && "$recovery_lab" == passed ]] && overall=passed
  completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  run_id=$(date -u +%Y%m%dT%H%M%SZ)
  payload=$(printf '{"data":{"run_id":"%s","status":"%s","completed_at":"%s","health_check":"%s","backup_evidence":"%s","recovery_lab":"%s"}}' \
    "$run_id" "$overall" "$completed_at" "$health_check" "$backup_evidence" "$recovery_lab")
  kubectl -n "$STATUS_NAMESPACE" patch configmap "$STATUS_CONFIGMAP" --type merge --patch "$payload" >/dev/null 2>&1 || true
}

trap 'report_status' EXIT

run_check() {
  local check_name=$1
  shift
  if "$@" >/dev/null 2>&1; then
    printf -v "$check_name" '%s' passed
  fi
}

check_k3s_and_portal() {
  kubectl get nodes --no-headers | grep -q ' Ready ' && \
    kubectl -n "$PORTAL_NAMESPACE" get deployment portal-web -o jsonpath='{.status.availableReplicas}' | grep -Eq '^[1-9][0-9]*$'
}

check_backup_evidence() {
  local evidence completed_at completed_epoch now_epoch
  evidence=$(kubectl -n "$PORTAL_NAMESPACE" get configmap portal-pvc-backup-evidence -o jsonpath='{.data.evidence}')
  printf '%s\n' "$evidence" | grep -qx 'source_runtime=k3s-pvc'
  completed_at=$(printf '%s\n' "$evidence" | sed -n 's/^backup_completed_at=//p' | head -n 1)
  completed_epoch=$(date -u -d "$completed_at" +%s)
  now_epoch=$(date -u +%s)
  (( completed_epoch <= now_epoch && now_epoch - completed_epoch <= BACKUP_MAX_AGE_SECONDS ))
}

cleanup_recovery_deployment() {
  if [[ "$recovery_deployment_created" == true ]]; then
    kubectl -n "$RECOVERY_NAMESPACE" delete deployment "$RECOVERY_DEPLOYMENT" --ignore-not-found=true >/dev/null 2>&1 || true
  fi
}

check_recovery_lab() {
  local pod before after deadline
  trap 'cleanup_recovery_deployment' RETURN
  kubectl -n "$RECOVERY_NAMESPACE" create -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${RECOVERY_DEPLOYMENT}
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: quarterly-sre-recovery-drill
  template:
    metadata:
      labels:
        app.kubernetes.io/name: quarterly-sre-recovery-drill
    spec:
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: recovery
          image: busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662
          command: ["sh", "-c", "sleep 3600 & while true; do sleep 3600; done"]
          securityContext:
            runAsNonRoot: true
            readOnlyRootFilesystem: true
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          livenessProbe:
            exec:
              command: ["sh", "-c", "test -f /proc/1/status"]
            initialDelaySeconds: 2
            periodSeconds: 2
EOF
  recovery_deployment_created=true
  kubectl -n "$RECOVERY_NAMESPACE" wait --for=condition=Available "deployment/${RECOVERY_DEPLOYMENT}" --timeout=90s
  pod=$(kubectl -n "$RECOVERY_NAMESPACE" get pods -l "$RECOVERY_LABEL" -o jsonpath='{.items[0].metadata.name}')
  before=$(kubectl -n "$RECOVERY_NAMESPACE" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}')
  kubectl -n "$RECOVERY_NAMESPACE" exec "$pod" -- sh -c 'kill 1'
  deadline=$((SECONDS + 90))
  while (( SECONDS < deadline )); do
    pod=$(kubectl -n "$RECOVERY_NAMESPACE" get pods -l "$RECOVERY_LABEL" -o jsonpath='{.items[0].metadata.name}')
    after=$(kubectl -n "$RECOVERY_NAMESPACE" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}')
    if (( after > before )) && kubectl -n "$RECOVERY_NAMESPACE" wait --for=condition=Ready "pod/${pod}" --timeout=5s >/dev/null; then
      kubectl -n "$RECOVERY_NAMESPACE" get events --field-selector "involvedObject.name=${pod}" >/dev/null
      return 0
    fi
    sleep 2
  done
  return 1
}

main() {
  configure_client
  run_check health_check check_k3s_and_portal
  run_check backup_evidence check_backup_evidence
  run_check recovery_lab check_recovery_lab
  [[ "$health_check" == passed && "$backup_evidence" == passed && "$recovery_lab" == passed ]]
}

main
