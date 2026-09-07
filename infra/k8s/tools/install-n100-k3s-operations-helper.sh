#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly HELPER_SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/n100-k3s-operations-helper.py"
readonly HELPER_DEST='/usr/local/libexec/personal-server/n100-k3s-operations'
readonly SUDOERS_DEST='/etc/sudoers.d/personal-server-n100-k3s-operations'

[[ "$EUID" -eq 0 ]] || { printf '%s\n' 'root 권한이 필요함' >&2; exit 1; }
[[ -f "$HELPER_SOURCE" && ! -L "$HELPER_SOURCE" ]] || { printf '%s\n' 'helper source가 regular file이 아님' >&2; exit 1; }
[[ "$(head -n 1 "$HELPER_SOURCE")" == '#!/usr/bin/python3' ]] || { printf '%s\n' 'helper interpreter가 고정되지 않음' >&2; exit 1; }

sudo_listing="$(sudo -l -U window 2>/dev/null)" || { printf '%s\n' 'window sudo 권한 사전 점검 실패' >&2; exit 1; }
if grep -Fq '/usr/local/bin/k3s' <<<"$sudo_listing"; then
  printf '%s\n' '일반 k3s sudo 권한이 남아 있어 설치를 중단함' >&2
  exit 1
fi

install -d -o root -g root -m 0755 "$(dirname "$HELPER_DEST")"
install -o root -g root -m 0755 "$HELPER_SOURCE" "$HELPER_DEST"

sudoers_tmp="$(mktemp)"
trap 'rm -f -- "$sudoers_tmp"' EXIT
chmod 0600 "$sudoers_tmp"
cat >"$sudoers_tmp" <<EOF
window ALL=(root) NOPASSWD: NOSETENV: $HELPER_DEST diagnose
window ALL=(root) NOPASSWD: NOSETENV: $HELPER_DEST verify_news_observability
window ALL=(root) NOPASSWD: NOSETENV: $HELPER_DEST apply_news_observability
EOF
visudo -cf "$sudoers_tmp" >/dev/null
install -o root -g root -m 0440 "$sudoers_tmp" "$SUDOERS_DEST"

grants="$(sudo -n -l -U window 2>/dev/null)" || { printf '%s\n' 'window sudo 권한 사후 점검 실패' >&2; exit 1; }
grep -Fq '/usr/local/bin/k3s' <<<"$grants" && { printf '%s\n' '일반 k3s sudo 권한이 남아 있어 설치를 중단함' >&2; exit 1; }
for operation in diagnose verify_news_observability apply_news_observability; do
  grep -Fq "$HELPER_DEST $operation" <<<"$grants" || { printf '%s\n' 'helper sudo 권한 사후 점검 실패' >&2; exit 1; }
done
