#!/usr/bin/env bash
set -u
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
CREDENTIAL_DIR=${TRANSITION_PREFLIGHT_CREDENTIAL_DIR:-/etc/personal-server-transition/credentials}
RUNNER="$REPO_ROOT/infra/k8s/transition-runner/runner/personal-server-transition-runner"
POLICY="$REPO_ROOT/infra/k8s/transition-runner/policy/runner-policy.json"
UNIT="$REPO_ROOT/infra/k8s/transition-runner/systemd/personal-server-transition.service"
VALIDATOR="$REPO_ROOT/infra/k8s/tools/validate-transition-runner-policy.py"
overall=0
check() { local name=$1 status=$2; printf 'check=%s status=%s\n' "$name" "$status"; [[ $status == PASS ]] || overall=1; }
metadata_ok() { local path=$1 mode owner; [[ -f $path && ! -L $path ]] || return 1; mode=$(stat -c '%a' -- "$path" 2>/dev/null) || return 1; owner=$(stat -c '%u' -- "$path" 2>/dev/null) || return 1; [[ $mode == 600 && $owner == 0 ]]; }
trusted_dir() { local path=$1 mode owner; [[ -d $path && ! -L $path ]] || return 1; while :; do mode=$(stat -c '%a' -- "$path" 2>/dev/null) || return 1; owner=$(stat -c '%u' -- "$path" 2>/dev/null) || return 1; [[ $owner == 0 && $((10#$mode & 022)) == 0 ]] || return 1; [[ $path == / ]] && break; path=$(dirname -- "$path"); done; }
for item in "$RUNNER" "$POLICY" "$UNIT" "$VALIDATOR"; do [[ -f $item && ! -L $item && -r $item ]] || overall=1; done
check release_artifacts "$([[ $overall -eq 0 ]] && echo PASS || echo FAIL)"
if [[ $overall -eq 0 ]]; then digest=$(sha256sum -- "$RUNNER" "$POLICY" "$VALIDATOR" "$UNIT" | sha256sum | awk '{print $1}'); check release_digest "$([[ $digest =~ ^[0-9a-f]{64}$ ]] && echo PASS || echo FAIL)"; else check release_digest FAIL; fi
if trusted_dir "$CREDENTIAL_DIR"; then check credential_directory PASS; else check credential_directory FAIL; fi
for credential in rclone-config rclone-config-passphrase age-identity; do if metadata_ok "$CREDENTIAL_DIR/$credential.cred"; then check "credential_$credential" PASS; else check "credential_$credential" FAIL; fi; done
for destination in /usr/local/libexec /etc /etc/systemd/system; do if trusted_dir "$destination" && [[ $(df -PT "$destination" 2>/dev/null | awk 'NR==2 {print $2}') == ext4 ]]; then check "destination_$(basename "$destination")" PASS; else check "destination_$(basename "$destination")" FAIL; fi; done
if (( overall == 0 )); then printf 'transition_runner_preflight=PASS\n'; else printf 'transition_runner_preflight=FAIL\n'; fi
exit "$overall"
