#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly HELPER_SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/n100-k3s-operations-helper.py"
readonly HELPER_DEST='/usr/local/libexec/personal-server/n100-k3s-operations'
readonly SUDOERS_DEST='/etc/sudoers.d/personal-server-n100-k3s-operations'

[[ "$EUID" -eq 0 ]] || { printf '%s\n' 'root 권한이 필요함' >&2; exit 1; }
[[ -f "$HELPER_SOURCE" && ! -L "$HELPER_SOURCE" ]] || { printf '%s\n' 'helper source가 regular file이 아님' >&2; exit 1; }

install -d -o root -g root -m 0755 "$(dirname "$HELPER_DEST")"
install -o root -g root -m 0755 "$HELPER_SOURCE" "$HELPER_DEST"

sudoers_tmp="$(mktemp)"
trap 'rm -f -- "$sudoers_tmp"' EXIT
chmod 0600 "$sudoers_tmp"
cat >"$sudoers_tmp" <<EOF
window ALL=(root) NOPASSWD: $HELPER_DEST diagnose
window ALL=(root) NOPASSWD: $HELPER_DEST verify_news_observability
window ALL=(root) NOPASSWD: $HELPER_DEST apply_news_observability
EOF
visudo -cf "$sudoers_tmp" >/dev/null
install -o root -g root -m 0440 "$sudoers_tmp" "$SUDOERS_DEST"
