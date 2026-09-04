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
[[ -d "$CREDENTIAL_DIR" && ! -L "$CREDENTIAL_DIR" ]] || fail
for credential in rclone-config rclone-config-passphrase age-identity; do
  path="$CREDENTIAL_DIR/$credential.cred"
  [[ -f "$path" && ! -L "$path" ]] || fail
  [[ $(stat -c '%a' -- "$path" 2>/dev/null) == 600 ]] || fail
  [[ $(stat -c '%u' -- "$path" 2>/dev/null) == 0 ]] || fail
done
[[ -r "$SOURCE_RUNNER" && -r "$SOURCE_POLICY" && -r "$SOURCE_UNIT" && -r "$SOURCE_VALIDATOR" ]] || fail
actual="sha256:$(sha256sum -- "$SOURCE_RUNNER" | awk '{print $1}')"
[[ "$actual" == "$RELEASE_DIGEST" ]] || fail

for parent in /usr/local /usr/local/libexec /etc /etc/systemd /etc/systemd/system; do
  [[ -d "$parent" && ! -L "$parent" ]] || fail
  [[ $(stat -c '%u:%a' -- "$parent") == 0:* ]] || fail
done
fs=$(df -PT /usr/local | awk 'NR==2 {print $2}')
[[ "$fs" == ext4 ]] || fail
[[ ! -e "$LIBEXEC" && ! -e "$TARGET_ETC" && ! -e "$UNIT_TARGET" ]] || fail

stage_libexec=$(mktemp -d /usr/local/libexec/.personal-server-transition.XXXXXX)
stage_etc=$(mktemp -d /etc/.personal-server-transition.XXXXXX)
stage_unit=$(mktemp /etc/systemd/.personal-server-transition.service.XXXXXX)
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
mv -- "$stage_libexec" "$LIBEXEC"
mv -- "$stage_etc" "$TARGET_ETC"
mv -- "$stage_unit" "$UNIT_TARGET"
trap - EXIT
printf 'transition_runner_install=PASS\n'
