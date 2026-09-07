#!/usr/bin/env bash
set +x
set -euo pipefail
umask 077
[[ "$EUID" -eq 0 ]] || { printf '%s\n' 'root 권한이 필요함' >&2; exit 1; }
readonly INSTALLER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/n100-k3s-operations-install.py"
exec /usr/bin/python3 -I "$INSTALLER" "$@"
