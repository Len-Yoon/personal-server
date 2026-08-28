#!/usr/bin/env bash
set -u -o pipefail

TIMEOUT_SECONDS=30
NS=""
SECRET_NAME=""
IMAGE_REF=""
DOCKER_TAG=""
ENV_FILE="${PORTAL_ENV_FILE:-/mnt/c/personal-server/.env}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
TMP_ENV=""
TMP_SECRET=""
NAMESPACE_TARGET=0
IMAGE_TARGET=0
DOCKER_TAG_TARGET=0

run_timeout() { timeout "${1}s" "${@:2}"; }
fail() { printf '%s\n' "portal_secret_shadow=FAIL" >&2; return 1; }
valid_run_id() { case "$1" in ''|*[!a-zA-Z0-9-]*) return 1;; *) return 0;; esac; }
configure_target() {
  local run_id_lc
  run_id_lc=$(printf '%s' "$RUN_ID" | tr '[:upper:]' '[:lower:]')
  NS="portal-secret-shadow-${run_id_lc}"
  SECRET_NAME="portal-web-shadow-runtime"
  DOCKER_TAG="personal-server-portal-web:shadow-${run_id_lc}"
  IMAGE_REF="docker.io/library/${DOCKER_TAG}"
}
assert_namespace_absent() { ! run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl get namespace "$NS" >/dev/null 2>&1; }
assert_image_absent() { ! run_timeout "$TIMEOUT_SECONDS" sudo k3s ctr -n k8s.io images ls -q | grep -Fqx "$IMAGE_REF"; }
docker_tag_exists() { run_timeout "$TIMEOUT_SECONDS" docker image inspect "$DOCKER_TAG" >/dev/null 2>&1; }
cleanup() {
  local ok=0
  if [ "$DOCKER_TAG_TARGET" -eq 1 ]; then
    if docker_tag_exists; then run_timeout "$TIMEOUT_SECONDS" docker image rm "$DOCKER_TAG" >/dev/null 2>&1 || ok=1; fi
    docker_tag_exists && ok=1
  fi
  if [ "$NAMESPACE_TARGET" -eq 1 ]; then
    run_timeout 120 sudo k3s kubectl delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1 || ok=1
    assert_namespace_absent || ok=1
  fi
  if [ "$IMAGE_TARGET" -eq 1 ]; then
    if ! assert_image_absent; then run_timeout "$TIMEOUT_SECONDS" sudo k3s ctr -n k8s.io images rm "$IMAGE_REF" >/dev/null 2>&1 || ok=1; fi
    assert_image_absent || ok=1
  fi
  [ -z "$TMP_ENV" ] || rm -f -- "$TMP_ENV"
  [ -z "$TMP_SECRET" ] || rm -f -- "$TMP_SECRET"
  return "$ok"
}
on_signal() { cleanup; printf '%s\n' "portal_secret_shadow=FAIL" >&2; exit 130; }

main() {
  if [ "${1:-}" = "--cleanup" ]; then
    [ "$#" -eq 2 ] && RUN_ID="$2" || { printf '%s\n' "usage: --cleanup RUN_ID" >&2; fail; return 1; }
    valid_run_id "$RUN_ID" || { printf '%s\n' "invalid RUN_ID" >&2; fail; return 1; }
    configure_target; NAMESPACE_TARGET=1; IMAGE_TARGET=1; DOCKER_TAG_TARGET=1
    if cleanup; then printf '%s\n' "portal_secret_shadow=PASS"; return 0; fi
    fail; return 1
  fi
  [ "$#" -eq 0 ] || { printf '%s\n' "usage: [--cleanup RUN_ID]" >&2; fail; return 1; }
  valid_run_id "$RUN_ID" || { printf '%s\n' "invalid RUN_ID" >&2; cleanup; fail; return 1; }
  configure_target; trap on_signal INT TERM HUP
  printf '%s\n' "portal_secret_shadow_run_id=$RUN_ID"
  TMP_ENV=$(mktemp); TMP_SECRET=$(mktemp); chmod 600 "$TMP_ENV" "$TMP_SECRET"
  local encryption_status
  encryption_status=$(run_timeout "$TIMEOUT_SECONDS" sudo k3s secrets-encrypt status 2>/dev/null) || { printf '%s\n' "unable to read K3s encryption status" >&2; cleanup; fail; return 1; }
  if ! grep -Fqx 'Encryption Status: Enabled' <<<"$encryption_status"; then printf '%s\n' "K3s Secret encryption is not enabled" >&2; cleanup; fail; return 1; fi
  if [ ! -f "$ENV_FILE" ]; then printf '%s\n' "portal env file not found" >&2; cleanup; fail; return 1; fi
  if ! awk -v out="$TMP_ENV" '
    BEGIN { split("DELETE_PASSWORD FILE_MANAGER_PASSWORD ADMIN_STATUS_PASSWORD FILE_MANAGER_ACCESS_PASSWORD", keys, " "); for (i in keys) wanted[keys[i]]=1 }
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    { equal=index($0, "="); if (!equal) next; key=$0; sub(/^[[:space:]]+/, "", key); sub(/[[:space:]]*=.*$/, "", key); if (!(key in wanted)) next; value=substr($0, equal+1); sub(/^[[:space:]]+/, "", value); if (value == "") { bad=1; next }; print key "=" value >> out; found[key]=1 }
    END { for (key in wanted) if (!(key in found)) bad=1; exit bad }
  ' "$ENV_FILE"; then printf '%s\n' "required portal Secret key missing or empty" >&2; cleanup; fail; return 1; fi
  if run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl get namespace "$NS" >/dev/null 2>&1 || run_timeout "$TIMEOUT_SECONDS" sudo k3s ctr -n k8s.io images ls -q | grep -Fqx "$IMAGE_REF"; then printf '%s\n' "RUN_ID collides with an existing namespace or image" >&2; cleanup; fail; return 1; fi
  if ! run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl create namespace "$NS" >/dev/null; then cleanup; fail; return 1; fi
  NAMESPACE_TARGET=1
  if ! run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NS" create secret generic "$SECRET_NAME" --from-env-file="$TMP_ENV" --dry-run=client -o yaml | awk '1; /^type: Opaque$/ { print "immutable: true" }' >"$TMP_SECRET"; then cleanup; fail; return 1; fi
  if ! run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl apply -f "$TMP_SECRET" >/dev/null; then cleanup; fail; return 1; fi
  rm -f -- "$TMP_SECRET"; TMP_SECRET=""
  if docker_tag_exists; then printf '%s\n' "temporary Docker tag already exists" >&2; cleanup; fail; return 1; fi
  if ! run_timeout "$TIMEOUT_SECONDS" docker tag personal-server-portal-web:latest "$DOCKER_TAG"; then cleanup; fail; return 1; fi
  DOCKER_TAG_TARGET=1
  if ! run_timeout 120 docker save "$DOCKER_TAG" | run_timeout 120 sudo k3s ctr -n k8s.io images import - >/dev/null; then cleanup; fail; return 1; fi
  IMAGE_TARGET=1
  if ! run_timeout "$TIMEOUT_SECONDS" docker image rm "$DOCKER_TAG" >/dev/null; then cleanup; fail; return 1; fi
  DOCKER_TAG_TARGET=0
  run_timeout 120 sudo k3s kubectl -n "$NS" apply -f - >/dev/null <<YAML
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
spec:
  podSelector: {}
  policyTypes:
    - Ingress
    - Egress
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: portal-web-shadow
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: portal-web-shadow
  template:
    metadata:
      labels:
        app: portal-web-shadow
    spec:
      automountServiceAccountToken: false
      containers:
        - name: portal-web
          image: $IMAGE_REF
          imagePullPolicy: Never
          envFrom:
            - secretRef:
                name: $SECRET_NAME
          env:
            - name: FILE_STORAGE_PATH
              value: /data/files
          ports:
            - containerPort: 8000
          volumeMounts:
            - name: files
              mountPath: /data/files
      volumes:
        - name: files
          emptyDir: {}
YAML
  if [ "$?" -ne 0 ]; then cleanup; fail; return 1; fi
  if ! run_timeout 120 sudo k3s kubectl -n "$NS" rollout status deployment/portal-web-shadow --timeout=120s >/dev/null; then cleanup; fail; return 1; fi
  local pod
  pod=$(run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NS" get pod -l app=portal-web-shadow -o jsonpath='{.items[0].metadata.name}') || { cleanup; fail; return 1; }
  if ! run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NS" exec "$pod" -- python -c 'import os,sys; keys=("DELETE_PASSWORD","FILE_MANAGER_PASSWORD","ADMIN_STATUS_PASSWORD","FILE_MANAGER_ACCESS_PASSWORD"); sys.exit(any(not os.environ.get(key) for key in keys))' >/dev/null; then cleanup; fail; return 1; fi
  if ! run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NS" exec "$pod" -- python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=10).read()' >/dev/null; then cleanup; fail; return 1; fi
  if cleanup; then printf '%s\n' "portal_secret_shadow=PASS"; return 0; fi
  fail; return 1
}
main "$@"
