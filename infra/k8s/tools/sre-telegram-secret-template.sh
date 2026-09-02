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
  printf '%s\n' '4. In an approved N100 private directory, copy infra/k8s/sre-telegram/alertmanager.yaml.tmpl to a temporary alertmanager.yaml file.'
  printf '%s\n' '5. Run chmod 600 on that temporary file. Keep its fixed credentials_file unchanged; enter the approved bearer value only as the runtime Secret key alertmanager_auth_token in the approved local workflow.'
  printf '%s\n' '   Do not print config or Secret values.'
  printf '%s\n' '6. Run sre-telegram-preflight.sh --alertmanager-config-file with that file path; it runs amtool and the fixed-template validator without printing the file.'
  printf '%s\n' '7. Using the approved N100 Secret manager workflow, seed exactly that validated file as the alertmanager.yaml key.'
  printf '%s\n' '8. Keep the same 0600 temporary file until sre-telegram-install.sh --apply --alertmanager-config-file returns.'
  printf '%s\n' '9. Immediately after that install command returns, remove the temporary file with the approved secure removal procedure.'
  printf '%s\n' '10. Do not paste, display, commit, or log Secret values. This guidance does not create or modify Secrets.'
}

main "$@"
