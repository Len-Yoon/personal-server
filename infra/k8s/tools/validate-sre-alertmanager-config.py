#!/usr/bin/env python3
"""Fail-closed validation for the SRE-only Alertmanager configuration."""

from pathlib import Path
from collections.abc import Hashable
import sys
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only where PyYAML is unavailable.
    yaml = None


if yaml is not None:

    class UniqueKeySafeLoader(yaml.SafeLoader):
        """Safe YAML loader that rejects duplicate keys in every mapping."""

        def construct_mapping(self, node, deep=False):
            self.flatten_mapping(node)
            mapping = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, Hashable):
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        "found unhashable key",
                        key_node.start_mark,
                    )
                if key in mapping:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        "found duplicate key",
                        key_node.start_mark,
                    )
                mapping[key] = self.construct_object(value_node, deep=deep)
            return mapping


RELAY_RECEIVER = "sre-telegram-relay"
NOOP_RECEIVER = "sre-telegram-noop"
RELAY_WEBHOOK_URL = "http://sre-telegram-relay.monitoring.svc:8080/alertmanager"
RELAY_CREDENTIALS_FILE = (
    "/etc/alertmanager/secrets/sre-telegram-relay-runtime/alertmanager_auth_token"
)
EXPECTED_GROUP_BY = [
    "alertname",
    "namespace",
    "pod",
    "deployment",
    "persistentvolumeclaim",
]
EXPECTED_MATCHERS = ['sre_telegram="true"']


def is_mapping(value: Any) -> bool:
    return isinstance(value, dict)


def has_exact_keys(value: Any, expected_keys: set[str]) -> bool:
    return is_mapping(value) and set(value) == expected_keys


def is_fixed_config(config: Any) -> bool:
    if not is_mapping(config):
        return False

    if set(config) != {"route", "receivers"}:
        return False

    route = config["route"]
    if not has_exact_keys(route, {"receiver", "group_by", "repeat_interval", "routes"}):
        return False
    if route["receiver"] != NOOP_RECEIVER:
        return False
    if route["group_by"] != EXPECTED_GROUP_BY:
        return False
    if route["repeat_interval"] != "4h":
        return False

    child_routes = route["routes"]
    if not isinstance(child_routes, list) or len(child_routes) != 1:
        return False
    child_route = child_routes[0]
    if not has_exact_keys(child_route, {"matchers", "receiver"}):
        return False
    if child_route["matchers"] != EXPECTED_MATCHERS:
        return False
    if child_route["receiver"] != RELAY_RECEIVER:
        return False

    receivers = config["receivers"]
    if not isinstance(receivers, list) or len(receivers) != 2:
        return False
    noop_receiver, receiver = receivers
    if not has_exact_keys(noop_receiver, {"name"}):
        return False
    if noop_receiver["name"] != NOOP_RECEIVER:
        return False
    if not has_exact_keys(receiver, {"name", "webhook_configs"}):
        return False
    if receiver["name"] != RELAY_RECEIVER:
        return False

    webhook_configs = receiver["webhook_configs"]
    if not isinstance(webhook_configs, list) or len(webhook_configs) != 1:
        return False
    webhook_config = webhook_configs[0]
    if not has_exact_keys(webhook_config, {"url", "send_resolved", "http_config"}):
        return False
    if webhook_config["url"] != RELAY_WEBHOOK_URL:
        return False
    if webhook_config["send_resolved"] is not True:
        return False

    http_config = webhook_config["http_config"]
    if not has_exact_keys(http_config, {"authorization"}):
        return False
    authorization = http_config["authorization"]
    if not has_exact_keys(authorization, {"type", "credentials_file"}):
        return False
    if authorization["type"] != "Bearer":
        return False
    return authorization["credentials_file"] == RELAY_CREDENTIALS_FILE


def fail(reason: str) -> int:
    print(reason, file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return fail("invalid_alertmanager_input")
    if yaml is None:
        return fail("invalid_alertmanager_validator")

    try:
        config_text = Path(argv[1]).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return fail("invalid_alertmanager_input")

    try:
        config = yaml.load(config_text, Loader=UniqueKeySafeLoader)
    except yaml.YAMLError:
        return fail("invalid_alertmanager_config")

    if not is_fixed_config(config):
        return fail("invalid_alertmanager_config")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
