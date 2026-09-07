#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly PROJECT_ROOT="${N100_OPERATIONS_PROJECT_ROOT:-/mnt/c/personal-server}"
readonly ACTIONS_ROOT="${N100_OPERATIONS_ACTIONS_ROOT:-$PROJECT_ROOT}"
readonly DEPLOY_SHA="${N100_OPERATIONS_DEPLOY_SHA:-}"
readonly PUBLIC_HEALTH_URL='https://len.pe.kr/health'
readonly NEWS_HEALTH_URL='http://127.0.0.1:8001/health'
readonly NEWS_MANIFEST="$ACTIONS_ROOT/infra/k8s/sre-telegram/crawler-news-observability.yaml"
readonly RULE_MANIFEST="$ACTIONS_ROOT/infra/k8s/sre-telegram/prometheus-rule.yaml"

fail_invalid() {
  printf '%s\n' 'n100_operation=invalid status=FAIL' >&2
  exit 2
}

fail_operation() {
  printf 'n100_operation=%s status=FAIL\n' "$1" >&2
  exit 1
}

check_command() {
  "$@" >/dev/null 2>&1
}

run_diagnose() {
  local failed=0
  local status
  printf '%s\n' 'n100_step=docker status=START'
  status=PASS
  check_command docker ps || { status=FAIL; failed=1; }
  printf 'n100_step=docker status=%s\n' "$status"

  printf '%s\n' 'n100_step=cloudflared status=START'
  status=PASS
  check_command pgrep -x cloudflared || { status=FAIL; failed=1; }
  printf 'n100_step=cloudflared status=%s\n' "$status"

  printf '%s\n' 'n100_step=local_health status=START'
  status=PASS
  check_command curl --fail --silent --show-error "$NEWS_HEALTH_URL" || { status=FAIL; failed=1; }
  printf 'n100_step=local_health status=%s\n' "$status"

  printf '%s\n' 'n100_step=public_health status=START'
  status=PASS
  check_command curl --fail --silent --show-error "$PUBLIC_HEALTH_URL" || { status=FAIL; failed=1; }
  printf 'n100_step=public_health status=%s\n' "$status"

  printf '%s\n' 'n100_step=k3s status=START'
  status=PASS
  check_command sudo -n k3s kubectl get nodes --no-headers || { status=FAIL; failed=1; }
  check_command sudo -n k3s kubectl -n personal-server get deployment/portal-web || { status=FAIL; failed=1; }
  printf 'n100_step=k3s status=%s\n' "$status"
  [[ "$failed" -eq 0 ]]
}

run_deploy_safe_crawler() {
  [[ "$DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]] || return 1
  [[ -d "$PROJECT_ROOT" && -x "$PROJECT_ROOT/scripts/deploy-n100-safe.sh" ]] || return 1
  printf '%s\n' 'n100_step=deploy_safe_crawler status=START'
  if ! (cd "$PROJECT_ROOT" && ./scripts/deploy-n100-safe.sh "$DEPLOY_SHA" crawler-worker >/dev/null 2>&1); then
    printf '%s\n' 'n100_step=deploy_safe_crawler status=FAIL' >&2
    return 1
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

  printf '%s\n' 'n100_step=secret_key status=START'
  check_command sudo -n k3s kubectl -n monitoring get secret crawler-news-metrics -o 'jsonpath={.data.bearer_token}' || return 1
  printf '%s\n' 'n100_step=secret_key status=PASS'

  printf '%s\n' 'n100_step=servicemonitor status=START'
  check_command sudo -n k3s kubectl -n monitoring get servicemonitor crawler-news-observability || return 1
  printf '%s\n' 'n100_step=servicemonitor status=PASS'

  printf '%s\n' 'n100_step=prometheusrule status=START'
  check_command sudo -n k3s kubectl -n monitoring get prometheusrule sre-telegram-k3s-alerts || return 1
  printf '%s\n' 'n100_step=prometheusrule status=PASS'
}

run_apply_news_observability() {
  [[ -f "$NEWS_MANIFEST" && -f "$RULE_MANIFEST" ]] || return 1
  local manifest
  for manifest in "$NEWS_MANIFEST" "$RULE_MANIFEST"; do
    printf 'n100_step=manifest_preflight file=%s status=START\n' "$(basename "$manifest")"
    check_command sudo -n k3s kubectl apply --dry-run=client -f "$manifest" || return 1
    printf 'n100_step=manifest_preflight file=%s status=PASS\n' "$(basename "$manifest")"
  done
  for manifest in "$NEWS_MANIFEST" "$RULE_MANIFEST"; do
    printf 'n100_step=manifest_apply file=%s status=START\n' "$(basename "$manifest")"
    check_command sudo -n k3s kubectl apply -f "$manifest" || return 1
    printf 'n100_step=manifest_apply file=%s status=PASS\n' "$(basename "$manifest")"
  done
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
