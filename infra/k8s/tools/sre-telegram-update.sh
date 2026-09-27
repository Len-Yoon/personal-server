#!/usr/bin/env bash
# Update only the existing relay Deployment image. Runtime state and Secrets are never applied.
set -Eeuo pipefail

NAMESPACE=monitoring
DEPLOYMENT=sre-telegram-relay
CONTAINER=relay
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
VERIFY_SCRIPT="$SCRIPT_DIR/sre-telegram-verify.sh"
MODE=""
TARGET_IMAGE=""
ORIGINAL_IMAGE=""
ORIGINAL_IMAGE_ID=""
DEPLOYMENT_UID=""
MUTATION_ATTEMPTED=0

usage() {
  printf 'usage: %s {--check|--dry-run|--apply} --image REPOSITORY@sha256:DIGEST\n' "$0" >&2
}

fail() {
  printf 'sre_telegram_update_stage=%s\n' "$1"
  printf 'sre_telegram_update=FAIL\n'
  return 1
}

valid_image() {
  [[ "$1" =~ ^[[:alnum:]_.:/-]+@sha256:[[:xdigit:]]{64}$ ]]
}

read_deployment() {
  sudo -n k3s kubectl -n "$NAMESPACE" get deployment "$DEPLOYMENT" -o json |
    python3 -c '
import json
import sys

deployment = json.load(sys.stdin)
metadata = deployment.get("metadata", {})
spec = deployment.get("spec", {})
status = deployment.get("status", {})
pod_spec = spec.get("template", {}).get("spec", {})
containers = pod_spec.get("containers", [])
if len(containers) != 1 or containers[0].get("name") != "relay":
    raise SystemExit(1)
fields = (
    metadata.get("uid"), metadata.get("resourceVersion"), containers[0].get("image"),
    spec.get("replicas"), status.get("availableReplicas", 0),
    spec.get("strategy", {}).get("type"), containers[0].get("imagePullPolicy"),
)
if any(value is None or isinstance(value, str) and (not value or "|" in value) for value in fields):
    raise SystemExit(1)
print("|".join(map(str, fields)))
'
}

load_deployment() {
  local snapshot
  snapshot=$(read_deployment) || return 1
  IFS='|' read -r current_uid current_version current_image current_replicas current_available current_strategy current_pull_policy <<< "$snapshot"
  [ -n "$current_uid" ] && [ -n "$current_version" ] && [ -n "$current_image" ]
}

image_imported() {
  local reference="$1" canonical="$1"
  case "$reference" in
    */*) ;;
    *) canonical="docker.io/library/$reference" ;;
  esac
  sudo -n k3s ctr -n k8s.io images list -q |
    grep -Fx -e "$reference" -e "$canonical" >/dev/null
}

ready_pods_use_image() {
  local wanted="$1"
  sudo -n k3s kubectl -n "$NAMESPACE" get pods -l app.kubernetes.io/name="$DEPLOYMENT" -o json |
    python3 -c '
import json
import sys

wanted = sys.argv[1]
pods = json.load(sys.stdin).get("items", [])
ready = 0
image_id = ""
for pod in pods:
    containers = pod.get("spec", {}).get("containers", [])
    statuses = pod.get("status", {}).get("containerStatuses", [])
    if len(containers) != 1 or containers[0].get("name") != "relay":
        raise SystemExit(1)
    for status in statuses:
        if status.get("name") == "relay" and status.get("ready"):
            if containers[0].get("image") != wanted or not status.get("imageID"):
                raise SystemExit(1)
            ready += 1
            image_id = status["imageID"]
if ready != 1:
    raise SystemExit(1)
print(image_id)
' "$wanted"
}

patch_image() {
  local version="$1" previous="$2" replacement="$3" payload
  payload=$(python3 - "$version" "$previous" "$replacement" <<'PY'
import json
import sys

version, previous, replacement = sys.argv[1:]
print(json.dumps([
    {"op": "test", "path": "/metadata/resourceVersion", "value": version},
    {"op": "test", "path": "/spec/template/spec/containers/0/image", "value": previous},
    {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": replacement},
], separators=(",", ":")))
PY
  ) || return 1
  sudo -n k3s kubectl -n "$NAMESPACE" patch deployment "$DEPLOYMENT" --type=json -p "$payload" >/dev/null
}

verify_image() {
  local wanted="$1" expected_image_id="${2:-}" observed_image_id
  sudo -n k3s kubectl -n "$NAMESPACE" rollout status "deployment/$DEPLOYMENT" --timeout=120s >/dev/null || return 1
  load_deployment || return 1
  [ "$current_uid" = "$DEPLOYMENT_UID" ] && [ "$current_image" = "$wanted" ] || return 1
  [ "$current_replicas" = 1 ] && [ "$current_available" = 1 ] || return 1
  observed_image_id=$(ready_pods_use_image "$wanted") || return 1
  if [ -n "$expected_image_id" ] && [ "$observed_image_id" != "$expected_image_id" ]; then
    return 1
  fi
  bash "$VERIFY_SCRIPT" >/dev/null
}

rollback() {
  if ! load_deployment || [ "$current_uid" != "$DEPLOYMENT_UID" ]; then
    printf 'sre_telegram_rollback=UNVERIFIED\n'
    return 1
  fi
  if [ "$current_image" = "$ORIGINAL_IMAGE" ]; then
    if verify_image "$ORIGINAL_IMAGE" "$ORIGINAL_IMAGE_ID"; then
      printf 'sre_telegram_rollback=NOT_NEEDED\n'
      return 0
    fi
    printf 'sre_telegram_rollback=UNVERIFIED\n'
    return 1
  fi
  if [ "$current_image" != "$TARGET_IMAGE" ]; then
    printf 'sre_telegram_rollback=UNVERIFIED\n'
    return 1
  fi
  if patch_image "$current_version" "$TARGET_IMAGE" "$ORIGINAL_IMAGE" && verify_image "$ORIGINAL_IMAGE" "$ORIGINAL_IMAGE_ID"; then
    printf 'sre_telegram_rollback=PASS\n'
    return 0
  fi
  printf 'sre_telegram_rollback=UNVERIFIED\n'
  return 1
}

on_exit() {
  local result=$?
  trap - EXIT
  if [ "$result" -ne 0 ] && [ "$MUTATION_ATTEMPTED" -eq 1 ]; then
    rollback || true
  fi
  exit "$result"
}

main() {
  if [ "$#" -ne 3 ] || [ "$2" != --image ]; then
    usage
    fail arguments
    return 2
  fi
  MODE="$1"
  TARGET_IMAGE="$3"
  case "$MODE" in
    --check|--dry-run|--apply) ;;
    *) usage; fail arguments; return 2 ;;
  esac
  if ! valid_image "$TARGET_IMAGE"; then
    fail image_reference
    return 2
  fi
  if ! load_deployment; then
    fail deployment_snapshot
    return 1
  fi
  DEPLOYMENT_UID="$current_uid"
  ORIGINAL_IMAGE="$current_image"
  if [ "$current_replicas" != 1 ] || [ "$current_available" != 1 ] ||
     [ "$current_strategy" != Recreate ] || [ "$current_pull_policy" != Never ]; then
    fail deployment_preflight
    return 1
  fi
  if ! image_imported "$ORIGINAL_IMAGE" || ! image_imported "$TARGET_IMAGE"; then
    fail image_preflight
    return 1
  fi
  if ! ORIGINAL_IMAGE_ID=$(ready_pods_use_image "$ORIGINAL_IMAGE") || ! bash "$VERIFY_SCRIPT" >/dev/null; then
    fail runtime_preflight
    return 1
  fi
  if [ "$MODE" != --apply ]; then
    printf 'sre_telegram_update=CHECK_PASS\n'
    return 0
  fi
  if [ "$ORIGINAL_IMAGE" = "$TARGET_IMAGE" ]; then
    printf 'sre_telegram_update=PASS\n'
    return 0
  fi
  MUTATION_ATTEMPTED=1
  trap on_exit EXIT
  trap 'exit 130' INT TERM
  if ! patch_image "$current_version" "$ORIGINAL_IMAGE" "$TARGET_IMAGE"; then
    fail deployment_patch
    return 1
  fi
  if ! verify_image "$TARGET_IMAGE"; then
    fail runtime_verify
    return 1
  fi
  MUTATION_ATTEMPTED=0
  trap - EXIT INT TERM
  printf 'sre_telegram_update=PASS\n'
}

main "$@"
