#!/usr/bin/env bash
set -u

main() {
  if [ "$#" -ne 0 ]; then
    printf 'usage: %s\n' "$0" >&2
    return 2
  fi

  printf '%s\n' 'N100-local Secret seed guidance (manual procedure only)'
  printf '%s\n' '1. On the approved N100 host, use the approved Secret manager or SOPS/age procedure.'
  printf '%s\n' '2. Seed namespace monitoring Secret sre-telegram-relay-runtime with key names only:'
  printf '%s\n' '   telegram_bot_token'
  printf '%s\n' '   allowed_chat_id'
  printf '%s\n' '   alertmanager_auth_token'
  printf '%s\n' '3. Seed namespace monitoring Secret sre-telegram-alertmanager-config with key name only:'
  printf '%s\n' '   alertmanager.yaml'
  printf '%s\n' '4. Do not paste, display, commit, or log Secret values. This guidance does not create or modify Secrets.'
}

main "$@"
