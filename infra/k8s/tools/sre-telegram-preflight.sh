#!/usr/bin/env bash
set -Eeuo pipefail

RELEASE="personal-server-monitoring"
NAMESPACE="monitoring"
RUNTIME_SECRET="sre-telegram-relay-runtime"
ALERTMANAGER_SECRET="sre-telegram-alertmanager-config"
PROMETHEUS_SERVICE="personal-server-monitoring-prometheus"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
IMAGE_DIR="$REPO_ROOT/sre-telegram-relay"
ALERTMANAGER_CONFIG_CONTRACT="${SRE_TELEGRAM_ALERTMANAGER_CONFIG_CONTRACT:-$REPO_ROOT/infra/k8s/sre-telegram/alertmanager-config.contract.yaml}"
ALERTMANAGER_CONFIG_FILE="${SRE_TELEGRAM_ALERTMANAGER_CONFIG_FILE:-}"
overall=0

fail_check() {
  overall=1
  printf 'check=%s status=FAIL reason=%s\n' "$1" "$2"
}

usage() {
  printf 'usage: %s [--alertmanager-config-file PATH]\n' "$0" >&2
  printf '%s\n' 'An N100 operator-supplied local Alertmanager config file is required; its contents are never printed.' >&2
}

secret_keys_present() {
  local secret_name="$1"
  shift
  local description key
  description=$(sudo k3s kubectl -n "$NAMESPACE" describe secret "$secret_name" 2>/dev/null) || return 1
  for key in "$@"; do
    printf '%s\n' "$description" | awk -v key="$key" '$1 == key ":" && $2 ~ /^[1-9][0-9]*$/ && $3 == "bytes" { found=1 } END { exit(found ? 0 : 1) }' || return 1
  done
}

alertmanager_contract_valid() {
  local contract="$ALERTMANAGER_CONFIG_CONTRACT"
  [ -r "$contract" ] || return 1
  local required
  for required in \
    'route:' \
    'group_by:' \
    'repeat_interval: 4h' \
    'sre_telegram="true"' \
    'receiver: sre-telegram-relay' \
    'url: http://sre-telegram-relay.monitoring.svc:8080/alertmanager' \
    'send_resolved: true' \
    'credentials_file: /etc/alertmanager/secrets/sre-telegram-relay-runtime/alertmanager_auth_token'; do
    grep -F -- "$required" "$contract" >/dev/null || return 1
  done
}

alertmanager_effective_config_structure_valid() {
  local config_file="$1"
  python3 - "$config_file" >/dev/null 2>&1 <<'PY'
import re
import sys

RELAY_RECEIVER = "sre-telegram-relay"
RELAY_URL = "http://sre-telegram-relay.monitoring.svc:8080/alertmanager"
BEARER_CREDENTIALS_FILE = (
    "/etc/alertmanager/secrets/sre-telegram-relay-runtime/alertmanager_auth_token"
)
MATCHER = re.compile(r"^sre_telegram\s*=\s*(?:[\"'])?(true|false)(?:[\"'])?$")
SRE_MATCHER = re.compile(r"^sre_telegram(?:\s*(?:=|!=|=~|!~).*)?$")


def fail() -> None:
    raise ValueError("invalid Alertmanager SRE route structure")


def matcher_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value.strip().lower()
    return "ambiguous"


def sre_telegram_matcher_state(route):
    values = []
    direct_match = route.get("match")
    if direct_match is not None:
        if not isinstance(direct_match, dict):
            fail()
        if "sre_telegram" in direct_match:
            values.append(matcher_value(direct_match["sre_telegram"]))

    direct_match_re = route.get("match_re")
    if direct_match_re is not None:
        if not isinstance(direct_match_re, dict):
            fail()
        if "sre_telegram" in direct_match_re:
            values.append("ambiguous")

    matchers = route.get("matchers")
    if matchers is not None:
        if not isinstance(matchers, list):
            fail()
        for matcher in matchers:
            if not isinstance(matcher, str):
                fail()
            normalized = matcher.strip().lower()
            if SRE_MATCHER.fullmatch(normalized):
                match = MATCHER.fullmatch(normalized)
                values.append(match.group(1) if match else "ambiguous")
    if not values:
        return "inherited"
    if any(value not in {"true", "false"} for value in values):
        return "ambiguous"
    if len(set(values)) != 1:
        return "ambiguous"
    return values[0]


def matches_sre_telegram_true(inherited_state, route):
    if inherited_state not in {"true", "false", "ambiguous"}:
        fail()
    matcher_state = sre_telegram_matcher_state(route)
    if inherited_state == "false" or matcher_state == "false":
        return "false"
    if inherited_state == "ambiguous" or matcher_state == "ambiguous":
        return "ambiguous"
    return "true"


def route_receiver(route, inherited_receiver):
    receiver = route.get("receiver", inherited_receiver)
    if receiver is not None and not isinstance(receiver, str):
        fail()
    return receiver


def terminal_receivers_for_sre_telegram_true(route, inherited_receiver, inherited_state):
    if not isinstance(route, dict):
        fail()
    state = matches_sre_telegram_true(inherited_state, route)
    if state == "false":
        return []
    if state != "true":
        fail()

    receiver = route_receiver(route, inherited_receiver)
    children = route.get("routes", [])
    if not isinstance(children, list):
        fail()

    terminal_receivers = []
    matched_child = False
    for child in children:
        child_receivers = terminal_receivers_for_sre_telegram_true(
            child, receiver, state
        )
        if not child_receivers:
            continue
        matched_child = True
        terminal_receivers.extend(child_receivers)
        child_continue = child.get("continue", False)
        if not isinstance(child_continue, bool):
            fail()
        if not child_continue:
            break

    if matched_child:
        return terminal_receivers
    if receiver is None:
        fail()
    return [receiver]


def receiver_is_valid(receivers):
    if not isinstance(receivers, list):
        return False
    relay_receivers = [receiver for receiver in receivers if isinstance(receiver, dict) and receiver.get("name") == RELAY_RECEIVER]
    if len(relay_receivers) != 1:
        return False
    webhooks = relay_receivers[0].get("webhook_configs")
    if not isinstance(webhooks, list) or len(webhooks) != 1:
        return False
    webhook = webhooks[0]
    if not isinstance(webhook, dict):
        return False
    authorization = webhook.get("http_config", {}).get("authorization")
    return (
        webhook.get("url") == RELAY_URL
        and webhook.get("send_resolved") is True
        and isinstance(authorization, dict)
        and authorization.get("type") == "Bearer"
        and authorization.get("credentials_file") == BEARER_CREDENTIALS_FILE
    )


try:
    import yaml
    with open(sys.argv[1], encoding="utf-8") as config_stream:
        config = yaml.safe_load(config_stream)
    if not isinstance(config, dict):
        fail()
    route = config.get("route")
    if not isinstance(route, dict):
        fail()
    if not isinstance(route.get("group_by"), list) or not route["group_by"]:
        fail()
    if route.get("repeat_interval") != "4h":
        fail()
    terminal_receivers = terminal_receivers_for_sre_telegram_true(route, None, "true")
    if not terminal_receivers or any(
        receiver != RELAY_RECEIVER for receiver in terminal_receivers
    ):
        fail()
    if not receiver_is_valid(config.get("receivers")):
        fail()
except Exception:
    sys.exit(1)
PY
}

alertmanager_effective_config_valid() {
  local config_file="$1"
  [ -n "$config_file" ] && [ -f "$config_file" ] && [ -r "$config_file" ] || return 1
  command -v amtool >/dev/null 2>&1 || return 1
  amtool check-config "$config_file" >/dev/null 2>&1 || return 1
  alertmanager_effective_config_structure_valid "$config_file"
}

monitoring_release_deployed() {
  local status
  status=$(helm status "$RELEASE" --namespace "$NAMESPACE" --output json 2>/dev/null) || return 1
  [[ "$status" =~ \"status\"[[:space:]]*:[[:space:]]*\"deployed\" ]]
}

all_prometheus_replicas_ready() {
  local ready_replicas="$1"
  printf '%s\n' "$ready_replicas" | awk 'NF { count++; if ($1 !~ /^[1-9][0-9]*$/) { bad=1 } } END { exit(count > 0 && !bad ? 0 : 1) }'
}

positive_replica_count() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

main() {
  local alertmanager_config_file="$ALERTMANAGER_CONFIG_FILE"
  case "$#" in
    0)
      ;;
    2)
      if [ "$1" = "--alertmanager-config-file" ]; then
        alertmanager_config_file="$2"
      else
        usage
        printf 'sre_telegram_preflight=FAIL\n'
        return 2
      fi
      ;;
    *)
      usage
      printf 'sre_telegram_preflight=FAIL\n'
      return 2
      ;;
  esac

  if [ -z "$alertmanager_config_file" ]; then
    usage
    printf 'sre_telegram_preflight=FAIL\n'
    return 2
  fi

  local nodes grafana_ready prometheus_ready
  if ! nodes=$(sudo k3s kubectl get nodes --no-headers 2>/dev/null); then
    fail_check k3s_nodes unavailable
  elif ! printf '%s\n' "$nodes" | awk 'NF < 2 || $2 !~ /^Ready(,SchedulingDisabled)?$/ { bad=1 } END { exit(NR > 0 && !bad ? 0 : 1) }'; then
    fail_check k3s_nodes not_ready
  fi

  if ! monitoring_release_deployed; then
    fail_check monitoring_release not_deployed
  fi

  if ! grafana_ready=$(sudo k3s kubectl -n "$NAMESPACE" get deployment "${RELEASE}-grafana" -o jsonpath='{.status.availableReplicas}' 2>/dev/null) || ! positive_replica_count "${grafana_ready:-0}"; then
    fail_check grafana unavailable
  fi
  if ! prometheus_ready=$(sudo k3s kubectl -n "$NAMESPACE" get statefulset -l app.kubernetes.io/name=prometheus -o jsonpath='{range .items[*]}{.status.readyReplicas}{"\n"}{end}' 2>/dev/null) || ! all_prometheus_replicas_ready "$prometheus_ready"; then
    fail_check prometheus unavailable
  fi
  if ! sudo k3s kubectl -n "$NAMESPACE" get service "$PROMETHEUS_SERVICE" >/dev/null 2>&1; then
    fail_check prometheus_service unavailable
  fi

  if ! command -v docker >/dev/null 2>&1 || [ ! -r "$IMAGE_DIR/Dockerfile" ] || [ ! -r "$IMAGE_DIR/app/main.py" ]; then
    fail_check image_build_prerequisites unavailable
  fi
  if ! sudo k3s ctr version >/dev/null 2>&1; then
    fail_check image_import_prerequisites unavailable
  fi

  if ! alertmanager_contract_valid; then
    fail_check alertmanager_config_contract invalid
  fi
  if ! alertmanager_effective_config_valid "$alertmanager_config_file"; then
    fail_check alertmanager_effective_config invalid_or_unavailable
  fi

  if ! secret_keys_present "$RUNTIME_SECRET" telegram_bot_token allowed_chat_id alertmanager_auth_token; then
    fail_check relay_runtime_secret missing_keys
  fi
  if ! secret_keys_present "$ALERTMANAGER_SECRET" alertmanager.yaml; then
    fail_check alertmanager_config_secret missing_keys
  fi

  if [ "$overall" -eq 0 ]; then
    printf 'sre_telegram_preflight=PASS\n'
    return 0
  fi
  printf 'sre_telegram_preflight=FAIL\n'
  return 1
}

main "$@"
