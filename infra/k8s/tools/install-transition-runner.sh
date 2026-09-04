#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
SOURCE_RUNNER="$REPO_ROOT/infra/k8s/transition-runner/runner/personal-server-transition-runner"
SOURCE_POLICY="$REPO_ROOT/infra/k8s/transition-runner/policy/runner-policy.json"
SOURCE_UNIT="$REPO_ROOT/infra/k8s/transition-runner/systemd/personal-server-transition.service"
SOURCE_VALIDATOR="$REPO_ROOT/infra/k8s/tools/validate-transition-runner-policy.py"
LIBEXEC=/usr/local/libexec/personal-server-transition
TARGET_ETC=/etc/personal-server-transition
UNIT_TARGET=/etc/systemd/system/personal-server-transition.service

fail() { printf 'transition_runner_install=FAIL\n' >&2; exit 1; }
[[ $(id -u) == 0 ]] || fail
[[ $# -eq 5 && $1 == --apply && $2 == --release-digest && $4 == --credential-dir ]] || fail
RELEASE_DIGEST=$3
CREDENTIAL_DIR=$5
[[ $RELEASE_DIGEST =~ ^sha256:[0-9a-f]{64}$ ]] || fail
trusted_dir() { local p=$1 m u; [[ -d $p && ! -L $p ]] || return 1; while :; do m=$(stat -c '%a' -- "$p") || return 1; u=$(stat -c '%u' -- "$p") || return 1; [[ $u == 0 && $((10#$m & 022)) == 0 ]] || return 1; [[ $p == / ]] && break; p=$(dirname -- "$p"); done; }
trusted_dir "$CREDENTIAL_DIR" || fail
for credential in rclone-config rclone-config-passphrase age-identity; do
  path="$CREDENTIAL_DIR/$credential.cred"
  [[ -f "$path" && ! -L "$path" ]] || fail
  [[ $(stat -c '%a' -- "$path" 2>/dev/null) == 600 ]] || fail
  [[ $(stat -c '%u' -- "$path" 2>/dev/null) == 0 ]] || fail
done
[[ -r "$SOURCE_RUNNER" && -r "$SOURCE_POLICY" && -r "$SOURCE_UNIT" && -r "$SOURCE_VALIDATOR" ]] || fail
actual="sha256:$(sha256sum -- "$SOURCE_RUNNER" "$SOURCE_POLICY" "$SOURCE_VALIDATOR" "$SOURCE_UNIT" | sha256sum | awk '{print $1}')"
[[ "$actual" == "$RELEASE_DIGEST" ]] || fail

for parent in /usr/local/libexec /etc /etc/systemd/system; do trusted_dir "$parent" || fail; [[ $(df -PT "$parent" | awk 'NR==2 {print $2}') == ext4 ]] || fail; done
[[ ! -e "$LIBEXEC" && ! -e "$TARGET_ETC" && ! -e "$UNIT_TARGET" ]] || fail

stage_libexec=$(mktemp -d /usr/local/libexec/.personal-server-transition.XXXXXX)
stage_etc=$(mktemp -d /etc/.personal-server-transition.XXXXXX)
stage_unit=$(mktemp /etc/systemd/system/.personal-server-transition.service.XXXXXX)
cleanup() { rm -rf -- "$stage_libexec" "$stage_etc" "$stage_unit"; }
trap cleanup EXIT
install -o root -g root -m 0750 "$SOURCE_RUNNER" "$stage_libexec/personal-server-transition-runner"
install -o root -g root -m 0755 "$SOURCE_VALIDATOR" "$stage_libexec/personal-server-transition-policy-validator"
install -o root -g root -m 0750 "$SOURCE_POLICY" "$stage_etc/runner-policy.json"
install -d -o root -g root -m 0700 "$stage_etc/credentials"
for credential in rclone-config rclone-config-passphrase age-identity; do
  install -o root -g root -m 0600 "$CREDENTIAL_DIR/$credential.cred" "$stage_etc/credentials/$credential.cred"
done
install -o root -g root -m 0644 "$SOURCE_UNIT" "$stage_unit"
installed_libexec=0; installed_etc=0; installed_unit=0
rollback() { if (( installed_unit )); then rm -f -- "$UNIT_TARGET"; fi; if (( installed_etc )); then rm -rf -- "$TARGET_ETC"; fi; if (( installed_libexec )); then rm -rf -- "$LIBEXEC"; fi; }
trap 'rollback; cleanup' ERR INT TERM
mv -- "$stage_libexec" "$LIBEXEC"; installed_libexec=1
mv -- "$stage_etc" "$TARGET_ETC"; installed_etc=1
mv -- "$stage_unit" "$UNIT_TARGET"; installed_unit=1
trap - EXIT
printf 'transition_runner_install=PASS\n'
