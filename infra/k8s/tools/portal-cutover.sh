#!/usr/bin/env bash
set -u -o pipefail

# Operator-only, Portal-only Compose -> K3s cutover.
# This script intentionally has no EXIT trap: every destructive step checks its
# result and invokes abort_cutover explicitly so a partial operation is visible.

TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-30}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
ENV_FILE="${PORTAL_ENV_FILE:-$REPO_ROOT/.env}"
SOURCE_DIR="${PORTAL_SOURCE_DIR:-$REPO_ROOT/data/files}"
STATE_SOURCE_DIR="${PORTAL_STATE_SOURCE_DIR:-$REPO_ROOT/data/portal-web-state}"
LEGACY_STATE_SOURCE_DIR="${PORTAL_LEGACY_STATE_SOURCE_DIR:-$REPO_ROOT/data/logs}"
BACKUP_EVIDENCE="${PORTAL_BACKUP_EVIDENCE:-$REPO_ROOT/.portal-backup-verified}"
BACKUP_MAX_AGE_SECONDS="${PORTAL_BACKUP_MAX_AGE_SECONDS:-86400}"
BACKUP_FILE="${ENV_FILE}.portal-cutover.${RUN_ID}.bak"
NAMESPACE="${PORTAL_NAMESPACE:-personal-server}"
PVC_NAME="${PORTAL_PVC_NAME:-portal-web-files-dynamic}"
PVC_CAPACITY="${PORTAL_FILES_CAPACITY:-1Gi}"
STATE_PVC_NAME="${PORTAL_STATE_PVC_NAME:-portal-web-state-dynamic}"
STATE_PVC_CAPACITY="${PORTAL_STATE_CAPACITY:-256Mi}"
IMAGE_REF="${PORTAL_IMAGE_REF:-personal-server-portal-web:latest}"
CADDY_CONTAINER="${CADDY_CONTAINER:-personal-server-caddy-1}"
BRIDGE_GATEWAY="${DOCKER_BRIDGE_GATEWAY:-}"
BRIDGE_COMPOSE_FILE="${PORTAL_BRIDGE_COMPOSE_FILE:-$REPO_ROOT/docker-compose.portal-bridge.yml}"
RUNTIME_MARKER="${PORTAL_RUNTIME_MARKER:-$REPO_ROOT/data/portal-runtime.mode}"
NODE_PORT=30080
COPY_POD="portal-web-files-copy-${RUN_ID}"
FILES_RESTORE_POD="portal-web-files-restore-${RUN_ID}"
STATE_RESTORE_POD="portal-web-state-restore-${RUN_ID}"
TMP_ENV=""
TMP_YAML=""
GO=0
SWITCH_CADDY=0
ROLLBACK_CADDY=0
MIGRATE_COMPOSE_STATE=0
CHECK_NODEPORT_PRIVATE=0
COMPOSE_STOPPED=0
K3S_TARGET=0
ENV_BACKUP_TARGET=0
SECRET_TARGET=0
IMAGE_TARGET=0
STATE_PVC_TARGET=0
FILES_PVC_TARGET=0
MIGRATION_FAILED=0
EXECUTOR_EXCLUDED=0

run_timeout() { timeout "${1}s" "${@:2}"; }
fail() { printf '%s\n' "portal_cutover=FAIL" >&2; return 1; }
valid_run_id() {
  case "$1" in
    ''|*[!a-zA-Z0-9-]*) return 1 ;;
    *) return 0 ;;
  esac
}
valid_image_ref() {
  case "$1" in
    ''|*[!a-zA-Z0-9._:/@-]*) return 1 ;;
    *) return 0 ;;
  esac
}
valid_capacity() {
  case "$1" in
    ''|*[!0-9MGTi]*) return 1 ;;
    *Mi|*Gi|*Ti) return 0 ;;
    *) return 1 ;;
  esac
}
valid_ipv4() {
  case "$1" in
    ''|*[!0-9.]*|.*|*.) return 1 ;;
  esac
  python3 -c 'import ipaddress,sys; ipaddress.IPv4Address(sys.argv[1])' "$1" >/dev/null 2>&1
}
set_runtime_marker() {
  local mode="$1" directory temporary
  case "$mode" in compose|cutover|k3s) ;; *) return 1 ;; esac
  directory=$(dirname -- "$RUNTIME_MARKER")
  mkdir -p -- "$directory" || return 1
  temporary=$(mktemp "$directory/.portal-runtime.mode.XXXXXX") || return 1
  chmod 600 "$temporary" || { rm -f -- "$temporary"; return 1; }
  printf '%s\n' "$mode" > "$temporary" || { rm -f -- "$temporary"; return 1; }
  mv -- "$temporary" "$RUNTIME_MARKER"
}

assert_no_k3s_writer() {
  local replicas pod_count
  if run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" get deployment portal-web >/dev/null 2>&1; then
    replicas=$(run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" get deployment portal-web -o jsonpath='{.spec.replicas}') || return 1
    [ "${replicas:-0}" = "0" ] || return 1
    pod_count=$(run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" get pod -l app.kubernetes.io/name=portal-web --field-selector=status.phase=Running -o name) || return 1
    [ -z "$pod_count" ] || return 1
  fi
  return 0
}

assert_compose_writer_running() {
  local running
  running=$(run_timeout "$TIMEOUT_SECONDS" docker inspect portal-web --format '{{.State.Running}}') || return 1
  [ "$running" = "true" ]
}

assert_compose_writer_stopped() {
  local running
  running=$(run_timeout "$TIMEOUT_SECONDS" docker inspect portal-web --format '{{.State.Running}}') || return 1
  [ "$running" = "false" ]
}

assert_local_compose_data_ready() {
  [ -d "$SOURCE_DIR" ] || return 1
  [ -d "$STATE_SOURCE_DIR" ] || return 1
  [ -f "$STATE_SOURCE_DIR/homeops.sqlite3" ] || return 1
  assert_sqlite_quick_check "$STATE_SOURCE_DIR/homeops.sqlite3"
}

assert_backup_evidence() {
  [ -s "$BACKUP_EVIDENCE" ] || return 1
  run_timeout "$TIMEOUT_SECONDS" python3 "$SCRIPT_DIR/validate-backup-evidence.py" \
    --evidence "$BACKUP_EVIDENCE" \
    --max-age-seconds "$BACKUP_MAX_AGE_SECONDS"
}

assert_bridge_gateway() {
  local actual_gateway
  valid_ipv4 "$BRIDGE_GATEWAY" || return 1
  actual_gateway=$(run_timeout "$TIMEOUT_SECONDS" docker network inspect bridge --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}') || return 1
  [ "$BRIDGE_GATEWAY" = "$actual_gateway" ] || return 1
  run_timeout "$TIMEOUT_SECONDS" ip -4 addr show | grep -Fq " $BRIDGE_GATEWAY/"
}

exclude_portal_from_executor() {
  # Recreate the executor before stopping Portal: a queued Docker operation must
  # not be able to revive the Compose writer during the ownership handoff.
  DOCKER_BRIDGE_GATEWAY="$BRIDGE_GATEWAY" \
  HOMEOPS_DOCKER_MANAGED_SERVICES="system-agent,crawler-worker,youtube-memo,book-memo,caddy,homeops-executor" \
  EXPECTED_CONTAINERS="crawler-worker,youtube-memo,book-memo,system-agent" \
    run_timeout 120 docker compose -f "$REPO_ROOT/docker-compose.yml" -f "$REPO_ROOT/docker-compose.n100.yml" -f "$BRIDGE_COMPOSE_FILE" up -d --no-deps --force-recreate \
      homeops-executor system-agent crawler-worker youtube-memo book-memo >/dev/null || return 1
  EXECUTOR_EXCLUDED=1
}

restore_compose_executor() {
  HOMEOPS_DOCKER_MANAGED_SERVICES="portal-web,system-agent,crawler-worker,youtube-memo,book-memo,caddy,homeops-executor" \
  EXPECTED_CONTAINERS="portal-web,crawler-worker,youtube-memo,book-memo,system-agent" \
    run_timeout 120 docker compose -f "$REPO_ROOT/docker-compose.yml" -f "$REPO_ROOT/docker-compose.n100.yml" up -d --no-deps --force-recreate \
      homeops-executor system-agent crawler-worker youtube-memo book-memo >/dev/null
}

preflight_bridge() {
  local endpoint
  assert_bridge_gateway || return 1
  [ -f "$BRIDGE_COMPOSE_FILE" ] || return 1
  for endpoint in 18010 18011 18001 18002 18003; do
    run_timeout "$TIMEOUT_SECONDS" curl --fail --silent --show-error --max-time 10 "http://${BRIDGE_GATEWAY}:${endpoint}/health" >/dev/null || return 1
  done
}

secret_allowlist() {
  local output="$1"
  awk -v out="$output" '
    BEGIN {
      split("DELETE_PASSWORD FILE_MANAGER_PASSWORD ADMIN_STATUS_PASSWORD FILE_MANAGER_ACCESS_PASSWORD PORTFOLIO_ADMIN_PASSWORD HOMEOPS_EXECUTOR_SHARED_SECRET HOMEOPS_SCHEDULER_SECRET HOMEOPS_TELEGRAM_BOT_TOKEN HOMEOPS_TELEGRAM_CHAT_ID", keys, " ")
      for (i in keys) wanted[keys[i]]=1
    }
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    {
      equal=index($0, "=")
      if (!equal) next
      key=$0
      sub(/^[[:space:]]+/, "", key)
      sub(/[[:space:]]*=.*$/, "", key)
      if (!(key in wanted)) next
      value=substr($0, equal+1)
      sub(/^[[:space:]]+/, "", value)
      if (value == "") { bad=1; next }
      if (key in found) { bad=1; next }
      print key "=" value >> out
      found[key]=1
    }
    END { for (key in wanted) if (!(key in found)) bad=1; close(out); exit bad }
  ' "$ENV_FILE"
}

tree_digest() {
  local directory="$1"
  (
    cd -- "$directory" || exit 1
    find . -type f -print0 | sort -z | xargs -0 sha256sum
  ) | sha256sum | awk '{print $1}'
}

assert_sqlite_quick_check() {
  run_timeout "$TIMEOUT_SECONDS" python3 -c 'import sqlite3,sys; connection=sqlite3.connect(sys.argv[1]); result=connection.execute("PRAGMA quick_check").fetchone()[0]; connection.close(); sys.exit(result != "ok")' "$1"
}

assert_pvc_sqlite_quick_check() {
  run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" exec "$COPY_POD" -- python -c 'import sqlite3,sys; connection=sqlite3.connect(sys.argv[1]); result=connection.execute("PRAGMA quick_check").fetchone()[0]; connection.close(); sys.exit(result != "ok")' /var/lib/portal/homeops.sqlite3
}

assert_named_pvc_sqlite_quick_check() {
  local pod="$1"
  run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" exec "$pod" -- python -c 'import sqlite3,sys; connection=sqlite3.connect(sys.argv[1]); result=connection.execute("PRAGMA quick_check").fetchone()[0]; connection.close(); sys.exit(result != "ok")' /var/lib/portal/homeops.sqlite3
}

copy_portal_state_files() {
  local source="$1" destination="$2" state_file
  for state_file in homeops.sqlite3 homeops.sqlite3-wal homeops.sqlite3-shm security-events.txt auth-rate-limit-state.json; do
    [ ! -f "$source/$state_file" ] || cp -p -- "$source/$state_file" "$destination/$state_file" || return 1
  done
}

portal_state_digest() {
  local directory="$1" state_file
  (
    cd -- "$directory" || exit 1
    for state_file in homeops.sqlite3 homeops.sqlite3-wal homeops.sqlite3-shm security-events.txt auth-rate-limit-state.json; do
      [ ! -f "$state_file" ] || sha256sum "$state_file"
    done
  ) | sha256sum | awk '{print $1}'
}

migrate_legacy_state_atomically() {
  local parent temporary source_digest destination_digest
  if [ -d "$STATE_SOURCE_DIR" ]; then
    [ -f "$STATE_SOURCE_DIR/homeops.sqlite3" ] && assert_sqlite_quick_check "$STATE_SOURCE_DIR/homeops.sqlite3"
    return $?
  fi
  [ -d "$LEGACY_STATE_SOURCE_DIR" ] && [ -f "$LEGACY_STATE_SOURCE_DIR/homeops.sqlite3" ] || return 1
  parent=$(dirname -- "$STATE_SOURCE_DIR")
  mkdir -p -- "$parent" || return 1
  temporary=$(mktemp -d "$parent/.portal-web-state.${RUN_ID}.XXXXXX") || return 1
  chmod 700 "$temporary" || { rm -rf -- "$temporary"; return 1; }
  if ! copy_portal_state_files "$LEGACY_STATE_SOURCE_DIR" "$temporary"; then rm -rf -- "$temporary"; return 1; fi
  source_digest=$(portal_state_digest "$LEGACY_STATE_SOURCE_DIR") || { rm -rf -- "$temporary"; return 1; }
  destination_digest=$(portal_state_digest "$temporary") || { rm -rf -- "$temporary"; return 1; }
  if [ -z "$source_digest" ] || [ "$source_digest" != "$destination_digest" ] || ! assert_sqlite_quick_check "$temporary/homeops.sqlite3"; then
    rm -rf -- "$temporary"
    return 1
  fi
  mv -- "$temporary" "$STATE_SOURCE_DIR"
}

migrate_compose_state() {
  # The state mount changed from data/logs.  Never let regular Compose create
  # a blank replacement: this explicit operation establishes a stopped writer,
  # validates the copied state, then recreates Portal against the new mount.
  if ! assert_compose_writer_running || ! assert_no_k3s_writer; then
    printf '%s\n' "Compose Portal must be the only active writer for state migration" >&2
    return 1
  fi
  if ! set_runtime_marker cutover; then return 1; fi
  if ! run_timeout 120 docker compose -f "$REPO_ROOT/docker-compose.yml" -f "$REPO_ROOT/docker-compose.n100.yml" stop portal-web >/dev/null; then return 1; fi
  COMPOSE_STOPPED=1
  if ! migrate_legacy_state_atomically; then
    printf '%s\n' "Portal state migration digest or HomeOps SQLite quick_check failed" >&2
    return 1
  fi
  if ! set_runtime_marker compose; then return 1; fi
  if ! run_timeout 120 docker compose -f "$REPO_ROOT/docker-compose.yml" -f "$REPO_ROOT/docker-compose.n100.yml" up -d --no-deps --force-recreate portal-web >/dev/null; then
    set_runtime_marker cutover || true
    return 1
  fi
  assert_compose_writer_running
}

restore_pvc_to_local() {
  local pvc_name mount_path destination pod_name required_file parent temporary backup remote_digest local_digest
  pvc_name="$1"
  mount_path="$2"
  destination="$3"
  pod_name="$4"
  required_file="$5"
  # Rollback is invoked in a new process, so it cannot rely on preparation
  # flags. A PVC is retained until its staged local copy verifies.
  run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" get pvc "$pvc_name" >/dev/null 2>&1 || return 1
  parent=$(dirname -- "$destination")
  mkdir -p -- "$parent" || return 1
  temporary=$(mktemp -d "$parent/.portal-pvc.restore.${RUN_ID}.XXXXXX") || return 1
  chmod 700 "$temporary" || { rm -rf -- "$temporary"; return 1; }
  if ! run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $pod_name
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  containers:
    - name: restore
      image: $IMAGE_REF
      imagePullPolicy: Never
      command: ["python", "-c", "import time; time.sleep(3600)"]
      volumeMounts:
        - name: data
          mountPath: $mount_path
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: $pvc_name
YAML
  then rm -rf -- "$temporary"; return 1; fi
  if ! run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/$pod_name" --timeout=120s >/dev/null; then
    run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" delete pod "$pod_name" --ignore-not-found --wait=true >/dev/null 2>&1 || true
    rm -rf -- "$temporary"
    return 1
  fi
  remote_digest=$(run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" exec "$pod_name" -- sh -c "cd '$mount_path' && find . -type f -print0 | sort -z | xargs -0 sha256sum" | sha256sum | awk '{print $1}') || remote_digest=""
  if [ -z "$remote_digest" ] || ! run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" exec "$pod_name" -- tar -C "$mount_path" -cf - . | tar -C "$temporary" -xf -; then
    run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" delete pod "$pod_name" --ignore-not-found --wait=true >/dev/null 2>&1 || true
    rm -rf -- "$temporary"
    return 1
  fi
  local_digest=$(tree_digest "$temporary") || local_digest=""
  if [ "$remote_digest" != "$local_digest" ] || { [ -n "$required_file" ] && [ ! -f "$temporary/$required_file" ]; }; then
    run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" delete pod "$pod_name" --ignore-not-found --wait=true >/dev/null 2>&1 || true
    rm -rf -- "$temporary"
    return 1
  fi
  if [ "$required_file" = "homeops.sqlite3" ] && ! assert_sqlite_quick_check "$temporary/$required_file"; then
    run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" delete pod "$pod_name" --ignore-not-found --wait=true >/dev/null 2>&1 || true
    rm -rf -- "$temporary"
    return 1
  fi
  run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" delete pod "$pod_name" --wait=true >/dev/null || { rm -rf -- "$temporary"; return 1; }
  backup="$parent/.$(basename -- "$destination").rollback.${RUN_ID}.bak"
  [ ! -e "$backup" ] || { rm -rf -- "$temporary"; return 1; }
  if [ -e "$destination" ] && ! mv -- "$destination" "$backup"; then rm -rf -- "$temporary"; return 1; fi
  if ! mv -- "$temporary" "$destination"; then
    [ ! -e "$backup" ] || mv -- "$backup" "$destination" || true
    return 1
  fi
  [ ! -e "$backup" ] || rm -rf -- "$backup"
}

restore_files_from_pvc() {
  restore_pvc_to_local "$PVC_NAME" /data/files "$SOURCE_DIR" "$FILES_RESTORE_POD" ""
}

restore_state_from_pvc() {
  restore_pvc_to_local "$STATE_PVC_NAME" /var/lib/portal "$STATE_SOURCE_DIR" "$STATE_RESTORE_POD" homeops.sqlite3
}

assert_namespace() {
  run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl get namespace "$NAMESPACE" >/dev/null 2>&1
}

stop_k3s_writer() {
  run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" scale deployment/portal-web --replicas=0 --ignore-not-found >/dev/null 2>&1 || return 1
  wait_for_k3s_writer_absent
}

wait_for_k3s_writer_absent() {
  # kubectl wait handles terminating Pods; the subsequent get is the final
  # assertion before any Compose writer is allowed to start.
  run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" wait --for=delete pod -l app.kubernetes.io/name=portal-web --timeout=120s >/dev/null 2>&1 || true
  local pod_count
  pod_count=$(run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" get pod -l app.kubernetes.io/name=portal-web -o name) || return 1
  [ -z "$pod_count" ]
}

start_compose_writer() {
  assert_no_k3s_writer || return 1
  assert_local_compose_data_ready || return 1
  run_timeout 120 docker compose -f "$REPO_ROOT/docker-compose.yml" -f "$REPO_ROOT/docker-compose.n100.yml" up -d --no-deps --force-recreate portal-web >/dev/null 2>&1
}

remove_partial_resources() {
  local ok=0 k3s_stopped=0 files_restored=0 state_restored=0 restore_ready=0
  if [ "$K3S_TARGET" -eq 1 ]; then
    if stop_k3s_writer; then k3s_stopped=1; else ok=1; fi
    if [ "$FILES_PVC_TARGET" -eq 1 ] && [ "$k3s_stopped" -eq 1 ]; then
      if restore_files_from_pvc; then files_restored=1; else ok=1; fi
    fi
    if [ "$STATE_PVC_TARGET" -eq 1 ] && [ "$k3s_stopped" -eq 1 ]; then
      if restore_state_from_pvc; then state_restored=1; else ok=1; fi
    fi
    if { [ "$FILES_PVC_TARGET" -eq 0 ] || [ "$files_restored" -eq 1 ]; } && { [ "$STATE_PVC_TARGET" -eq 0 ] || [ "$state_restored" -eq 1 ]; }; then restore_ready=1; fi
    run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" delete pod "$COPY_POD" --ignore-not-found --wait=true >/dev/null 2>&1 || ok=1
    run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" delete deployment portal-web --ignore-not-found --wait=true >/dev/null 2>&1 || ok=1
    run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" delete service portal-web --ignore-not-found --wait=true >/dev/null 2>&1 || ok=1
    run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" delete endpointslice -l app.kubernetes.io/part-of=portal-compose-bridge --ignore-not-found --wait=true >/dev/null 2>&1 || ok=1
    run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" delete service -l app.kubernetes.io/part-of=portal-compose-bridge --ignore-not-found --wait=true >/dev/null 2>&1 || ok=1
    if [ "$restore_ready" -eq 1 ]; then
      [ "$FILES_PVC_TARGET" -eq 0 ] || run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" delete pvc "$PVC_NAME" --ignore-not-found --wait=true >/dev/null 2>&1 || ok=1
      [ "$STATE_PVC_TARGET" -eq 0 ] || run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" delete pvc "$STATE_PVC_NAME" --ignore-not-found --wait=true >/dev/null 2>&1 || ok=1
    else
      ok=1
    fi
    if [ "$SECRET_TARGET" -eq 1 ]; then
      run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" delete secret portal-web-runtime --ignore-not-found --wait=true >/dev/null 2>&1 || ok=1
    fi
    if [ "$IMAGE_TARGET" -eq 1 ]; then
      run_timeout "$TIMEOUT_SECONDS" sudo k3s ctr -n k8s.io images rm "$IMAGE_REF" >/dev/null 2>&1 || ok=1
    fi
    if [ "$k3s_stopped" -eq 1 ]; then wait_for_k3s_writer_absent || { ok=1; k3s_stopped=0; }; fi
  fi
  if [ "$COMPOSE_STOPPED" -eq 1 ] && [ "$MIGRATION_FAILED" -eq 0 ] && [ "$k3s_stopped" -eq 1 ] && [ "$restore_ready" -eq 1 ]; then
    if stop_k3s_writer && restore_compose_executor && start_compose_writer; then set_runtime_marker compose || ok=1; else ok=1; fi
  fi
  return "$ok"
}

abort_cutover() {
  # Do not publish compose mode until remove_partial_resources has stopped the
  # K3s writer and restored the state PVC.  Bootstrap treats cutover as a
  # writer boundary and must not race the rollback.
  remove_partial_resources || true
  [ -z "$TMP_ENV" ] || rm -f -- "$TMP_ENV"
  [ -z "$TMP_YAML" ] || rm -f -- "$TMP_YAML"
  if [ "$MIGRATION_FAILED" -eq 1 ]; then
    printf '%s\n' "Portal cutover paused after state migration failure; cutover marker retained and Compose Portal not started" >&2
  else
    printf '%s\n' "Portal cutover aborted; Compose Portal restored and K3s writer stopped" >&2
  fi
  fail
}

restore_writers_after_switch_failure() {
  if stop_k3s_writer; then
    if restore_files_from_pvc && restore_state_from_pvc; then
      restore_compose_executor || true
      if start_compose_writer; then set_runtime_marker compose || true; fi
    fi
  fi
}

on_signal() { abort_cutover; exit 130; }

set_portal_upstream() {
  local upstream="$1" temp_file
  temp_file=$(mktemp "$ENV_FILE.portal-cutover.XXXXXX") || return 1
  chmod 600 "$temp_file" || { rm -f -- "$temp_file"; return 1; }
  if ! awk -v value="$upstream" '
    /^[[:space:]]*#/ { print; next }
    /^[[:space:]]*PORTAL_UPSTREAM[[:space:]]*=/ { if (!done) { print "PORTAL_UPSTREAM=" value; done=1 }; next }
    { print }
    END { if (!done) print "PORTAL_UPSTREAM=" value }
  ' "$ENV_FILE" >"$temp_file"; then
    rm -f -- "$temp_file"
    return 1
  fi
  if ! mv -- "$temp_file" "$ENV_FILE"; then
    rm -f -- "$temp_file"
    return 1
  fi
  return 0
}

recreate_caddy() {
  run_timeout 120 docker compose -f "$REPO_ROOT/docker-compose.yml" -f "$REPO_ROOT/docker-compose.n100.yml" up -d --force-recreate --no-deps caddy >/dev/null
}

validate_nodeport() {
  # Caddy's cutover target is deliberately fixed at host.docker.internal:30080.
  run_timeout "$TIMEOUT_SECONDS" docker exec "$CADDY_CONTAINER" curl --fail --silent --show-error --max-time 10 "http://host.docker.internal:30080/health" | grep -Fq '"status":"ok"'
}

assert_nodeport_private_exposure() {
  local address
  # This verifies the node's own non-bridge addresses cannot reach the
  # NodePort. It deliberately does not claim to configure or validate an
  # upstream Windows/router firewall; without a restricted K3s NodePort bind,
  # the cutover refuses to publish traffic.
  assert_bridge_gateway || return 1
  while IFS= read -r address; do
    [ -n "$address" ] || continue
    [ "$address" = "$BRIDGE_GATEWAY" ] && continue
    if run_timeout "$TIMEOUT_SECONDS" curl --fail --silent --show-error --max-time 5 "http://${address}:${NODE_PORT}/health" >/dev/null; then
      return 1
    fi
  done < <(ip -4 -o addr show scope global | awk '{split($4, cidr, "/"); print cidr[1]}')
}

validate_public_hosts() {
  local host
  for host in len.pe.kr portfolio.len.pe.kr file.len.pe.kr admin.len.pe.kr; do
    if ! run_timeout "$TIMEOUT_SECONDS" curl --fail --silent --show-error --max-time 20 --resolve "$host:443:127.0.0.1" "https://${host}/health" | grep -Fq '"status":"ok"'; then
      return 1
    fi
  done
}

rollback_caddy() {
  # rollback restores PORTAL_UPSTREAM=portal-web:8000 before Compose is started.
  # rollback uses docker compose only for caddy recreation and Portal restore.
  # rollback uses kubectl scale to keep the K3s writer stopped.
  [ -f "$ENV_FILE" ] || { printf '%s\n' "portal env file not found" >&2; fail; return 1; }
  if ! set_runtime_marker cutover || ! stop_k3s_writer; then fail; return 1; fi
  if ! restore_files_from_pvc; then
    printf '%s\n' "Portal files PVC restore digest failed" >&2
    fail
    return 1
  fi
  if ! restore_state_from_pvc; then
    printf '%s\n' "Portal state PVC restore digest or HomeOps SQLite quick_check failed" >&2
    fail
    return 1
  fi
  if ! restore_compose_executor || ! start_compose_writer; then fail; return 1; fi
  if ! set_portal_upstream "portal-web:8000"; then fail; return 1; fi
  if ! recreate_caddy; then fail; return 1; fi
  if ! validate_public_hosts; then fail; return 1; fi
  if ! set_runtime_marker compose; then fail; return 1; fi
  printf '%s\n' "portal_cutover=PASS"
}

switch_caddy() {
  local previous_upstream
  previous_upstream=$(awk -F= '$1 == "PORTAL_UPSTREAM" { value=$2 } END { print value }' "$ENV_FILE") || return 1
  previous_upstream="${previous_upstream:-portal-web:8000}"
  if [ -e "$BACKUP_FILE" ]; then
    printf '%s\n' "cutover backup already exists for RUN_ID" >&2
    return 1
  fi
  if ! cp -- "$ENV_FILE" "$BACKUP_FILE"; then return 1; fi
  chmod 600 "$BACKUP_FILE" || return 1
  ENV_BACKUP_TARGET=1
  if ! set_portal_upstream "host.docker.internal:${NODE_PORT}"; then return 1; fi
  if ! recreate_caddy || ! validate_nodeport || ! validate_public_hosts; then
    set_portal_upstream "$previous_upstream" || true
    recreate_caddy || true
    restore_writers_after_switch_failure
    fail
    return 1
  fi
  if ! set_runtime_marker k3s; then
    set_portal_upstream "$previous_upstream" || true
    recreate_caddy || true
    restore_writers_after_switch_failure
    fail
    return 1
  fi
  printf '%s\n' "portal_cutover=PASS"
}

switch_prepared_caddy() {
  local replicas
  if ! assert_namespace; then printf '%s\n' "K3s namespace not found" >&2; fail; return 1; fi
  if ! assert_compose_writer_stopped; then
    printf '%s\n' "Compose Portal is still running; refusing public switch" >&2
    fail
    return 1
  fi
  # A stopped Compose Portal is the expected state after the --go preparation.
  COMPOSE_STOPPED=1
  replicas=$(run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" get deployment portal-web -o jsonpath='{.spec.replicas}') || { printf '%s\n' "prepared K3s Portal deployment not found" >&2; restore_writers_after_switch_failure; fail; return 1; }
  [ "$replicas" = "1" ] || { printf '%s\n' "prepared K3s Portal deployment is not running" >&2; restore_writers_after_switch_failure; fail; return 1; }
  if ! run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" rollout status deployment/portal-web --timeout=120s >/dev/null || ! validate_nodeport || ! assert_nodeport_private_exposure; then
    printf '%s\n' "prepared K3s Portal failed health validation" >&2
    restore_writers_after_switch_failure
    fail
    return 1
  fi
  switch_caddy || { restore_writers_after_switch_failure; return 1; }
}

prepare_cutover() {
  local encryption_status source_digest destination_digest state_source_digest state_destination_digest pod
  trap on_signal INT TERM HUP
  printf '%s\n' "portal_cutover_run_id=$RUN_ID"

  # Encryption is checked before reading the env file or touching resources.
  encryption_status=$(run_timeout "$TIMEOUT_SECONDS" sudo k3s secrets-encrypt status 2>/dev/null) || { printf '%s\n' "unable to read K3s encryption status" >&2; fail; return 1; }
  if ! grep -Fqx 'Encryption Status: Enabled' <<<"$encryption_status"; then
    printf '%s\n' "K3s Secret encryption is not enabled" >&2
    fail
    return 1
  fi
  if ! assert_backup_evidence; then printf '%s\n' "encrypted backup evidence is missing or invalid" >&2; fail; return 1; fi
  if ! valid_capacity "$PVC_CAPACITY" || ! valid_capacity "$STATE_PVC_CAPACITY"; then printf '%s\n' "invalid Portal PVC capacity" >&2; fail; return 1; fi
  if ! valid_image_ref "$IMAGE_REF"; then printf '%s\n' "invalid Portal image reference" >&2; fail; return 1; fi
  if [ ! -d "$SOURCE_DIR" ]; then printf '%s\n' "Portal source data directory not found" >&2; fail; return 1; fi
  if [ -d "$STATE_SOURCE_DIR" ] && [ ! -f "$STATE_SOURCE_DIR/homeops.sqlite3" ]; then printf '%s\n' "Portal state directory is missing HomeOps SQLite" >&2; fail; return 1; fi
  if [ ! -d "$STATE_SOURCE_DIR" ] && { [ ! -d "$LEGACY_STATE_SOURCE_DIR" ] || [ ! -f "$LEGACY_STATE_SOURCE_DIR/homeops.sqlite3" ]; }; then printf '%s\n' "Portal state source is missing" >&2; fail; return 1; fi
  if [ ! -f "$ENV_FILE" ]; then printf '%s\n' "portal env file not found" >&2; fail; return 1; fi
  if ! assert_namespace; then printf '%s\n' "K3s namespace not found" >&2; fail; return 1; fi
  if ! assert_compose_writer_running; then printf '%s\n' "Compose Portal is not the current writer" >&2; fail; return 1; fi
  if ! assert_no_k3s_writer; then printf '%s\n' "a K3s Portal writer already exists" >&2; fail; return 1; fi
  if ! assert_bridge_gateway; then printf '%s\n' "Docker bridge gateway is missing, invalid, or does not match the bridge network" >&2; fail; return 1; fi
  if ! exclude_portal_from_executor; then printf '%s\n' "unable to exclude Portal from HomeOps Docker control before cutover" >&2; fail; return 1; fi
  if ! preflight_bridge; then
    restore_compose_executor || true
    printf '%s\n' "Compose bridge endpoint preflight failed" >&2
    fail
    return 1
  fi
  if run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" get pvc "$PVC_NAME" >/dev/null 2>&1; then
    printf '%s\n' "Portal PVC already exists; refusing to overwrite it" >&2
    fail
    return 1
  fi
  if run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" get pvc "$STATE_PVC_NAME" >/dev/null 2>&1; then
    printf '%s\n' "Portal state PVC already exists; refusing to overwrite it" >&2
    fail
    return 1
  fi
  if run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" get secret portal-web-runtime >/dev/null 2>&1; then
    printf '%s\n' "Portal runtime Secret already exists; refusing to overwrite it" >&2
    fail
    return 1
  fi
  if run_timeout "$TIMEOUT_SECONDS" sudo k3s ctr -n k8s.io images ls -q | grep -Fqx "$IMAGE_REF"; then
    printf '%s\n' "Portal image already exists in K3s; refusing to overwrite it" >&2
    fail
    return 1
  fi
  TMP_ENV=$(mktemp)
  chmod 600 "$TMP_ENV" || { rm -f -- "$TMP_ENV"; TMP_ENV=""; fail; return 1; }
  if ! secret_allowlist "$TMP_ENV"; then
    rm -f -- "$TMP_ENV"; TMP_ENV=""
    printf '%s\n' "required Portal Secret key missing, duplicated, or empty" >&2
    fail
    return 1
  fi
  source_digest=$(tree_digest "$SOURCE_DIR") || { abort_cutover; return 1; }
  if ! run_timeout "$TIMEOUT_SECONDS" docker image inspect "$IMAGE_REF" >/dev/null 2>&1; then printf '%s\n' "Portal image not found locally" >&2; abort_cutover; return 1; fi
  K3S_TARGET=1
  if ! run_timeout 120 docker save "$IMAGE_REF" | run_timeout 120 sudo k3s ctr -n k8s.io images import - >/dev/null; then abort_cutover; return 1; fi
  IMAGE_TARGET=1

  # Marker is set before stopping Compose so bootstrap cannot recreate a writer.
  if ! set_runtime_marker cutover; then printf '%s\n' "unable to set Portal cutover runtime marker" >&2; fail; return 1; fi
  # Stop exactly the Compose Portal service before copying its files.
  if ! run_timeout 120 docker compose -f "$REPO_ROOT/docker-compose.yml" -f "$REPO_ROOT/docker-compose.n100.yml" stop portal-web >/dev/null; then
    set_runtime_marker compose || true
    abort_cutover
    return 1
  fi
  COMPOSE_STOPPED=1
  if ! migrate_legacy_state_atomically; then
    MIGRATION_FAILED=1
    printf '%s\n' "Portal state migration digest or HomeOps SQLite quick_check failed" >&2
    abort_cutover
    return 1
  fi
  state_source_digest=$(tree_digest "$STATE_SOURCE_DIR") || { abort_cutover; return 1; }
  if ! assert_sqlite_quick_check "$STATE_SOURCE_DIR/homeops.sqlite3"; then printf '%s\n' "Portal HomeOps SQLite source failed quick_check" >&2; abort_cutover; return 1; fi
  if ! run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $PVC_NAME
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: local-path
  resources:
    requests:
      storage: $PVC_CAPACITY
YAML
  then abort_cutover; return 1; fi
  if ! run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" wait --for=jsonpath='{.status.phase}'=Bound "pvc/$PVC_NAME" --timeout=120s >/dev/null; then abort_cutover; return 1; fi
  FILES_PVC_TARGET=1
  if ! run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $STATE_PVC_NAME
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: local-path
  resources:
    requests:
      storage: $STATE_PVC_CAPACITY
YAML
  then abort_cutover; return 1; fi
  if ! run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" wait --for=jsonpath='{.status.phase}'=Bound "pvc/$STATE_PVC_NAME" --timeout=120s >/dev/null; then abort_cutover; return 1; fi
  STATE_PVC_TARGET=1
  if ! run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $COPY_POD
  labels:
    app.kubernetes.io/name: portal-web-files-copy
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  containers:
    - name: copier
      image: $IMAGE_REF
      imagePullPolicy: Never
      command: ["python", "-c", "import time; time.sleep(3600)"]
      volumeMounts:
        - name: files
          mountPath: /data/files
        - name: state
          mountPath: /var/lib/portal
  volumes:
    - name: files
      persistentVolumeClaim:
        claimName: $PVC_NAME
    - name: state
      persistentVolumeClaim:
        claimName: $STATE_PVC_NAME
YAML
  then abort_cutover; return 1; fi
  if ! run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/$COPY_POD" --timeout=120s >/dev/null; then abort_cutover; return 1; fi
  if ! tar -C "$SOURCE_DIR" -cf - . | run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" exec -i "$COPY_POD" -- tar -xf - -C /data/files; then abort_cutover; return 1; fi
  if ! tar -C "$STATE_SOURCE_DIR" -cf - . | run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" exec -i "$COPY_POD" -- tar -xf - -C /var/lib/portal; then abort_cutover; return 1; fi
  destination_digest=$(run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" exec "$COPY_POD" -- sh -c 'cd /data/files && find . -type f -print0 | sort -z | xargs -0 sha256sum' | sha256sum | awk '{print $1}') || { abort_cutover; return 1; }
  if [ -z "$source_digest" ] || [ "$source_digest" != "$destination_digest" ]; then
    printf '%s\n' "Portal PVC digest does not match the stopped Compose source" >&2
    abort_cutover
    return 1
  fi
  state_destination_digest=$(run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" exec "$COPY_POD" -- sh -c 'cd /var/lib/portal && find . -type f -print0 | sort -z | xargs -0 sha256sum' | sha256sum | awk '{print $1}') || { abort_cutover; return 1; }
  if [ -z "$state_source_digest" ] || [ "$state_source_digest" != "$state_destination_digest" ] || ! assert_pvc_sqlite_quick_check; then
    printf '%s\n' "Portal state PVC digest or HomeOps SQLite quick_check failed" >&2
    abort_cutover
    return 1
  fi
  if ! run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" delete pod "$COPY_POD" --wait=true >/dev/null; then abort_cutover; return 1; fi
  if ! run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Service
metadata:
  name: compose-system-agent
  labels:
    app.kubernetes.io/part-of: portal-compose-bridge
spec:
  ports:
    - name: http
      port: 8010
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: compose-system-agent-bridge
  labels:
    kubernetes.io/service-name: compose-system-agent
    app.kubernetes.io/part-of: portal-compose-bridge
addressType: IPv4
ports:
  - name: http
    protocol: TCP
    port: 18010
endpoints:
  - addresses:
      - $BRIDGE_GATEWAY
---
apiVersion: v1
kind: Service
metadata:
  name: compose-homeops-executor
  labels:
    app.kubernetes.io/part-of: portal-compose-bridge
spec:
  ports:
    - name: http
      port: 8011
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: compose-homeops-executor-bridge
  labels:
    kubernetes.io/service-name: compose-homeops-executor
    app.kubernetes.io/part-of: portal-compose-bridge
addressType: IPv4
ports:
  - name: http
    protocol: TCP
    port: 18011
endpoints:
  - addresses:
      - $BRIDGE_GATEWAY
---
apiVersion: v1
kind: Service
metadata:
  name: compose-crawler
  labels:
    app.kubernetes.io/part-of: portal-compose-bridge
spec:
  ports:
    - name: http
      port: 8001
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: compose-crawler-bridge
  labels:
    kubernetes.io/service-name: compose-crawler
    app.kubernetes.io/part-of: portal-compose-bridge
addressType: IPv4
ports:
  - name: http
    protocol: TCP
    port: 18001
endpoints:
  - addresses:
      - $BRIDGE_GATEWAY
---
apiVersion: v1
kind: Service
metadata:
  name: compose-youtube
  labels:
    app.kubernetes.io/part-of: portal-compose-bridge
spec:
  ports:
    - name: http
      port: 8002
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: compose-youtube-bridge
  labels:
    kubernetes.io/service-name: compose-youtube
    app.kubernetes.io/part-of: portal-compose-bridge
addressType: IPv4
ports:
  - name: http
    protocol: TCP
    port: 18002
endpoints:
  - addresses:
      - $BRIDGE_GATEWAY
---
apiVersion: v1
kind: Service
metadata:
  name: compose-book
  labels:
    app.kubernetes.io/part-of: portal-compose-bridge
spec:
  ports:
    - name: http
      port: 8003
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: compose-book-bridge
  labels:
    kubernetes.io/service-name: compose-book
    app.kubernetes.io/part-of: portal-compose-bridge
addressType: IPv4
ports:
  - name: http
    protocol: TCP
    port: 18003
endpoints:
  - addresses:
      - $BRIDGE_GATEWAY
YAML
  then abort_cutover; return 1; fi
  if ! run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" create secret generic portal-web-runtime --from-env-file="$TMP_ENV" --dry-run=client -o yaml | awk '1; /^type: Opaque$/ { print "immutable: true" }' | run_timeout 120 sudo k3s kubectl apply -f - >/dev/null; then abort_cutover; return 1; fi
  SECRET_TARGET=1
  rm -f -- "$TMP_ENV"; TMP_ENV=""
  if ! run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" apply -f - >/dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: portal-web
  labels:
    app.kubernetes.io/name: portal-web
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: portal-web
  template:
    metadata:
      labels:
        app.kubernetes.io/name: portal-web
    spec:
      automountServiceAccountToken: false
      containers:
        - name: portal-web
          image: $IMAGE_REF
          imagePullPolicy: Never
          command:
            - uvicorn
            - app.main:app
            - --host
            - 0.0.0.0
            - --port
            - "8000"
            - --workers
            - "1"
            - --no-access-log
          envFrom:
            - secretRef:
                name: portal-web-runtime
          env:
            - name: FILE_STORAGE_PATH
              value: /data/files
            - name: HOMEOPS_DB_PATH
              value: /var/lib/portal/homeops.sqlite3
            - name: SECURITY_LOG_PATH
              value: /var/lib/portal/security-events.txt
            - name: AUTH_RATE_LIMIT_STATE_PATH
              value: /var/lib/portal/auth-rate-limit-state.json
            - name: SYSTEM_AGENT_URL
              value: http://compose-system-agent:8010
            - name: SYSTEM_AGENT_HEALTH_URL
              value: http://compose-system-agent:8010/health
            - name: HOMEOPS_EXECUTOR_URL
              value: http://compose-homeops-executor:8011
            - name: NEWS_SEARCH_URL
              value: http://compose-crawler:8001/api/search
            - name: NEWS_HEALTH_URL
              value: http://compose-crawler:8001/health
            - name: YOUTUBE_SEARCH_URL
              value: http://compose-youtube:8002/api/search
            - name: YOUTUBE_HEALTH_URL
              value: http://compose-youtube:8002/health
            - name: BOOKS_SEARCH_URL
              value: http://compose-book:8003/api/search
            - name: BOOKS_HEALTH_URL
              value: http://compose-book:8003/health
          ports:
            - name: http
              containerPort: 8000
          readinessProbe:
            httpGet:
              path: /health
              port: http
          livenessProbe:
            httpGet:
              path: /health
              port: http
          volumeMounts:
            - name: files
              mountPath: /data/files
            - name: state
              mountPath: /var/lib/portal
      volumes:
        - name: files
          persistentVolumeClaim:
            claimName: $PVC_NAME
        - name: state
          persistentVolumeClaim:
            claimName: $STATE_PVC_NAME
YAML
  then abort_cutover; return 1; fi
  if ! run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Service
metadata:
  name: portal-web
spec:
  type: NodePort
  selector:
    app.kubernetes.io/name: portal-web
  ports:
    - name: http
      protocol: TCP
      port: 8000
      targetPort: http
      nodePort: 30080
YAML
  then abort_cutover; return 1; fi
  if ! run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" rollout status deployment/portal-web --timeout=120s >/dev/null; then abort_cutover; return 1; fi
  pod=$(run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" get pod -l app.kubernetes.io/name=portal-web -o jsonpath='{.items[0].metadata.name}') || { abort_cutover; return 1; }
  if [ -z "$pod" ] || ! run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" exec "$pod" -- python -c 'import os,sys; keys=("DELETE_PASSWORD","FILE_MANAGER_PASSWORD","ADMIN_STATUS_PASSWORD","FILE_MANAGER_ACCESS_PASSWORD"); sys.exit(any(not os.environ.get(key) for key in keys))' >/dev/null; then abort_cutover; return 1; fi
  if ! run_timeout "$TIMEOUT_SECONDS" sudo k3s kubectl -n "$NAMESPACE" exec "$pod" -- python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=10).read()' >/dev/null; then abort_cutover; return 1; fi
  if ! validate_nodeport; then abort_cutover; return 1; fi
  # The NodePort exists before Caddy changes.  Refuse preparation itself when
  # it is reachable on any non-bridge host address.
  if ! assert_nodeport_private_exposure; then abort_cutover; return 1; fi
  printf '%s\n' "portal_cutover=PASS"
}

usage() {
  printf '%s\n' "usage: portal-cutover.sh --check-nodeport-private | portal-cutover.sh --migrate-compose-state | portal-cutover.sh --go | portal-cutover.sh --go --switch-caddy | portal-cutover.sh --rollback-caddy" >&2
}

main() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --go) GO=1 ;;
      --switch-caddy) SWITCH_CADDY=1 ;;
      --rollback-caddy) ROLLBACK_CADDY=1 ;;
      --migrate-compose-state) MIGRATE_COMPOSE_STATE=1 ;;
      --check-nodeport-private) CHECK_NODEPORT_PRIVATE=1 ;;
      --help|-h) usage; return 0 ;;
      *) usage; fail; return 1 ;;
    esac
    shift
  done
  valid_run_id "$RUN_ID" || { printf '%s\n' "invalid RUN_ID" >&2; fail; return 1; }
  if [ "$CHECK_NODEPORT_PRIVATE" -eq 1 ] && { [ "$MIGRATE_COMPOSE_STATE" -eq 1 ] || [ "$GO" -eq 1 ] || [ "$SWITCH_CADDY" -eq 1 ] || [ "$ROLLBACK_CADDY" -eq 1 ]; }; then usage; fail; return 1; fi
  if [ "$MIGRATE_COMPOSE_STATE" -eq 1 ] && { [ "$GO" -eq 1 ] || [ "$SWITCH_CADDY" -eq 1 ] || [ "$ROLLBACK_CADDY" -eq 1 ]; }; then usage; fail; return 1; fi
  if [ "$ROLLBACK_CADDY" -eq 1 ] && [ "$SWITCH_CADDY" -eq 1 ]; then usage; fail; return 1; fi
  if [ "$CHECK_NODEPORT_PRIVATE" -eq 1 ]; then
    if assert_nodeport_private_exposure; then printf '%s\n' "portal_nodeport_private=PASS"; return 0; fi
    printf '%s\n' "portal_nodeport_private=FAIL" >&2
    return 1
  fi
  if [ "$MIGRATE_COMPOSE_STATE" -eq 1 ]; then migrate_compose_state; return $?; fi
  if [ "$ROLLBACK_CADDY" -eq 1 ]; then rollback_caddy; return $?; fi
  if [ "$GO" -ne 1 ]; then
    printf '%s\n' "explicit --go is required; no operation performed" >&2
    usage
    fail
    return 1
  fi
  if [ "$SWITCH_CADDY" -eq 1 ]; then switch_prepared_caddy; return $?; fi
  prepare_cutover
}

main "$@"
