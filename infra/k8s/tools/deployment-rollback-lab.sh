#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
MANIFEST="$SCRIPT_DIR/../observability-lab/rollback-lab.yaml"
NAMESPACE=deployment-rollback-lab
NAME=rollback-sample
mode=""
expected_context=""
dirty=false

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --check|--go) [[ -z "$mode" ]] || fail 'supply one mode'; mode="$1" ;;
    --context) shift; (($#)) || fail 'context is required'; expected_context="$1" ;;
    *) fail 'usage: deployment-rollback-lab.sh --check|--go --context CURRENT_CONTEXT' ;;
  esac
  shift
done
[[ -n "$mode" && "$expected_context" =~ ^[A-Za-z0-9][A-Za-z0-9._:/@-]*$ ]] || fail 'mode and valid context are required'
[[ -r "$MANIFEST" ]] || fail 'baseline manifest unavailable'
active_context="$(sudo -n k3s kubectl --request-timeout=15s config current-context 2>/dev/null)" || fail 'context lookup failed'
[[ "$active_context" == "$expected_context" ]] || fail 'context mismatch'
kubectl=(sudo -n k3s kubectl --context "$expected_context" --request-timeout=15s)

owned_or_absent() {
  local payload
  payload="$("${kubectl[@]}" "$@" --ignore-not-found=true -o json 2>/dev/null)" || return 1
  [[ -z "$payload" ]] && return 0
  printf '%s' "$payload" | python3 -c 'import json,sys; p=json.load(sys.stdin); sys.exit(0 if p.get("metadata",{}).get("labels",{}).get("app.kubernetes.io/managed-by")=="deployment-rollback-lab" else 1)' >/dev/null 2>&1
}

ownership() {
  owned_or_absent get namespace "$NAMESPACE" &&
    owned_or_absent -n "$NAMESPACE" get deployment "$NAME"
}

healthy() {
  local response
  "${kubectl[@]}" --request-timeout=100s -n "$NAMESPACE" rollout status "deployment/$NAME" --timeout=90s >/dev/null 2>&1 || return 1
  response="$("${kubectl[@]}" -n "$NAMESPACE" exec "deployment/$NAME" -- wget -qO- -T 3 http://127.0.0.1:8080/ 2>/dev/null)" || return 1
  [[ "$response" == 'rollback lab sample ready' ]]
}

restore() {
  # Recheck the ownership boundary even on an interrupt; never use rollout undo.
  ownership &&
    "${kubectl[@]}" apply -f "$MANIFEST" >/dev/null 2>&1 && healthy
}

finish() {
  local status=$?
  trap - EXIT INT TERM
  if [[ "$dirty" == true ]]; then
    if restore; then
      printf 'rollback_restore=PASS\n'
    else
      printf 'rollback_restore=FAIL\n'
      printf 'baseline restore failed; inspect the dedicated lab before retry\n' >&2
      status=1
    fi
  fi
  if ((status != 0)); then printf 'deployment_rollback_lab=FAIL\n'; fi
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ownership || fail 'dedicated resource ownership mismatch or lookup failed'
if [[ "$mode" == --check ]]; then
  printf 'deployment_rollback_lab=CHECK_PASS\n'
  exit 0
fi

# Namespace creation is included in this fixed baseline. No existing data is deleted.
"${kubectl[@]}" apply -f "$MANIFEST" >/dev/null 2>&1 || fail 'baseline apply failed; inspect live state before retry'
healthy || fail 'baseline health failed; fault not injected'
printf 'rollback_baseline=PASS\n'

# Set the cleanup guard before the request: a failed response may have applied the patch.
dirty=true
"${kubectl[@]}" -n "$NAMESPACE" patch deployment "$NAME" --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/exec/command","value":["/bin/sh","-c","exit 1"]}]' >/dev/null 2>&1 \
  || fail 'fault injection response failed'
"${kubectl[@]}" --request-timeout=70s -n "$NAMESPACE" wait --for=condition=Available=false "deployment/$NAME" --timeout=60s >/dev/null 2>&1 \
  || fail 'unavailable condition not detected'
# Available=false alone can precede controller observation of the new template.
"${kubectl[@]}" --request-timeout=70s -n "$NAMESPACE" rollout status "deployment/$NAME" --timeout=60s >/dev/null 2>&1 &&
  fail 'fault deployment unexpectedly completed'
"${kubectl[@]}" -n "$NAMESPACE" get deployment "$NAME" -o json 2>/dev/null \
  | python3 -c 'import json,sys; p=json.load(sys.stdin); s=p.get("status",{}); sys.exit(0 if s.get("observedGeneration",0)>=p["metadata"]["generation"] and s.get("updatedReplicas",0)==1 and s.get("readyReplicas",0)==0 else 1)' >/dev/null 2>&1 \
  || fail 'new unavailable revision not confirmed'
printf 'rollback_failure_detected=PASS\n'
if restore; then
  dirty=false
  printf 'rollback_restore=PASS\ndeployment_rollback_lab=PASS\n'
else
  # Do not repeat a failed restore automatically; report the first failure.
  dirty=false
  printf 'rollback_restore=FAIL\n'
  fail 'baseline restore failed; inspect the dedicated lab before retry'
fi
