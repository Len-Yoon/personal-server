#!/usr/bin/env bash
set -u

# Read-only N100 gate.  Deliberately emits names and statuses, never file data.
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
CREDENTIAL_DIR=${TRANSITION_PREFLIGHT_CREDENTIAL_DIR:-/etc/personal-server-transition/credentials}
RUNNER="$REPO_ROOT/infra/k8s/transition-runner/runner/personal-server-transition-runner"
POLICY="$REPO_ROOT/infra/k8s/transition-runner/policy/runner-policy.json"
VALIDATOR="$REPO_ROOT/infra/k8s/tools/validate-transition-runner-policy.py"
overall=0

check() {
  local name=$1 status=$2
  printf 'check=%s status=%s\n' "$name" "$status"
  [[ "$status" == PASS ]] || overall=1
}

regular_0600() {
  local path=$1 mode owner
  [[ -f "$path" && ! -L "$path" ]] || return 1
  mode=$(stat -c '%a' -- "$path" 2>/dev/null || stat -f '%Lp' -- "$path" 2>/dev/null) || return 1
  owner=$(stat -c '%u' -- "$path" 2>/dev/null || stat -f '%u' -- "$path" 2>/dev/null) || return 1
  [[ "$mode" == 600 && "$owner" == 0 ]]
}

check artifact status=$( [[ -r "$RUNNER" && -r "$POLICY" && -r "$VALIDATOR" ]] && echo PASS || echo FAIL )
if command -v sha256sum >/dev/null 2>&1 && [[ -r "$RUNNER" ]]; then
  check release_digest status=$(sha256sum -- "$RUNNER" >/dev/null && echo PASS || echo FAIL)
else
  check release_digest FAIL
fi
if [[ -x "$VALIDATOR" ]]; then
  python3 "$VALIDATOR" "$POLICY" >/dev/null 2>&1
  check policy status=$([[ $? -eq 0 ]] && echo PASS || echo FAIL)
else
  check policy FAIL
fi
for credential in rclone-config rclone-config-passphrase age-identity; do
  if regular_0600 "$CREDENTIAL_DIR/$credential.cred"; then
    check "credential_$credential" PASS
  else
    check "credential_$credential" FAIL
  fi
done

if [[ -d /usr/local && ! -L /usr/local ]]; then
  fs=$(df -PT /usr/local 2>/dev/null | awk 'NR==2 {print $2}')
  check native_ext4 status=$([[ "$fs" == ext4 ]] && echo PASS || echo FAIL)
else
  check native_ext4 FAIL
fi

if (( overall == 0 )); then
  printf 'transition_runner_preflight=PASS\n'
else
  printf 'transition_runner_preflight=FAIL\n'
fi
exit "$overall"
