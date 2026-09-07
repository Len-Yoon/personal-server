#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

readonly PROJECT_ROOT='/mnt/c/personal-server'
readonly HELPER='/usr/local/libexec/personal-server/n100-k3s-operations'
readonly ACTIONS_ROOT="${N100_OPERATIONS_ACTIONS_ROOT:-}"
readonly DEPLOY_SHA="${N100_OPERATIONS_DEPLOY_SHA:-}"
readonly PUBLIC_HEALTH_URL='https://len.pe.kr/health'
readonly NEWS_HEALTH_URL='http://127.0.0.1:8001/health'

fail_invalid() { printf '%s\n' 'n100_operation=invalid status=FAIL' >&2; exit 2; }
fail_operation() { printf 'n100_operation=%s status=FAIL\n' "$1" >&2; exit 1; }
check_command() { "$@" >/dev/null 2>&1; }

validated_manifest() {
  local candidate="$1" root resolved
  [[ -n "$ACTIONS_ROOT" && -d "$ACTIONS_ROOT" && ! -L "$ACTIONS_ROOT" ]] || return 1
  root="$(realpath -- "$ACTIONS_ROOT")" || return 1
  [[ -d "$root" ]] || return 1
  [[ -f "$candidate" && ! -L "$candidate" ]] || return 1
  resolved="$(realpath -- "$candidate")" || return 1
  [[ "$resolved" == "$root"/* && -f "$resolved" && ! -L "$resolved" ]] || return 1
  printf '%s\n' "$resolved"
}

run_diagnose() {
  local failed=0 status
  printf '%s\n' 'n100_step=docker status=START'
  status=PASS; check_command docker ps || { status=FAIL; failed=1; }; printf 'n100_step=docker status=%s\n' "$status"
  printf '%s\n' 'n100_step=cloudflared status=START'
  status=PASS; check_command pgrep -x cloudflared || { status=FAIL; failed=1; }; printf 'n100_step=cloudflared status=%s\n' "$status"
  printf '%s\n' 'n100_step=local_health status=START'
  status=PASS; check_command curl --fail --silent --show-error "$NEWS_HEALTH_URL" || { status=FAIL; failed=1; }; printf 'n100_step=local_health status=%s\n' "$status"
  printf '%s\n' 'n100_step=public_health status=START'
  status=PASS; check_command curl --fail --silent --show-error "$PUBLIC_HEALTH_URL" || { status=FAIL; failed=1; }; printf 'n100_step=public_health status=%s\n' "$status"
  printf '%s\n' 'n100_step=k3s status=START'
  status=PASS; check_command sudo -n "$HELPER" diagnose || { status=FAIL; failed=1; }; printf 'n100_step=k3s status=%s\n' "$status"
  [[ "$failed" -eq 0 ]]
}

run_deploy_safe_crawler() {
  [[ "$DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]] || return 1
  [[ -x "$PROJECT_ROOT/scripts/deploy-n100-safe.sh" ]] || return 1
  printf '%s\n' 'n100_step=deploy_safe_crawler status=START'
  if ! (cd "$PROJECT_ROOT" && ./scripts/deploy-n100-safe.sh "$DEPLOY_SHA" crawler-worker >/dev/null 2>&1); then
    printf '%s\n' 'n100_step=deploy_safe_crawler status=FAIL' >&2; return 1
  fi
  printf '%s\n' 'n100_step=deploy_safe_crawler status=PASS'
}

run_verify_news_observability() {
  printf '%s\n' 'n100_step=news_health status=START'
  check_command curl --fail --silent --show-error "$NEWS_HEALTH_URL" || return 1
  printf '%s\n' 'n100_step=news_health status=PASS'
  printf '%s\n' 'n100_step=runtime_token status=START'
  check_command docker exec crawler-worker sh -c 'test -n "${NEWS_METRICS_BEARER_TOKEN:-}"' || return 1
  printf '%s\n' 'n100_step=runtime_token status=PASS'
  printf '%s\n' 'n100_step=k3s status=START'
  check_command sudo -n "$HELPER" verify_news_observability || return 1
  printf '%s\n' 'n100_step=k3s status=PASS'
}

run_apply_news_observability() {
  local news_manifest rule_manifest
  news_manifest="$(validated_manifest "$ACTIONS_ROOT/infra/k8s/sre-telegram/crawler-news-observability.yaml")" || return 1
  rule_manifest="$(validated_manifest "$ACTIONS_ROOT/infra/k8s/sre-telegram/prometheus-rule.yaml")" || return 1
  printf '%s\n' 'n100_step=manifest_apply status=START'
  if ! { cat -- "$news_manifest"; printf '\n---\n'; cat -- "$rule_manifest"; } | sudo -n "$HELPER" apply_news_observability >/dev/null 2>&1; then
    printf '%s\n' 'n100_step=manifest_apply status=FAIL' >&2; return 1
  fi
  printf '%s\n' 'n100_step=manifest_apply status=PASS'
}

[[ "$#" -eq 1 ]] || fail_invalid
operation="$1"
case "$operation" in
  diagnose|deploy_safe_crawler|verify_news_observability|apply_news_observability) ;;
  *) fail_invalid ;;
esac
case "$operation" in
  diagnose) run_diagnose || fail_operation "$operation" ;;
  deploy_safe_crawler) run_deploy_safe_crawler || fail_operation "$operation" ;;
  verify_news_observability) run_verify_news_observability || fail_operation "$operation" ;;
  apply_news_observability) run_apply_news_observability || fail_operation "$operation" ;;
esac
printf 'n100_operation=%s status=PASS\n' "$operation"
