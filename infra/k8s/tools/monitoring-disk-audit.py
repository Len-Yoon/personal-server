#!/usr/bin/env python3
"""Read-only disk metric/rule coverage audit; never sends a synthetic alert.

Exit 0 requires complete observed coverage. Exit 1 means a coverage gap or an
observed disk warning; exit 2 means evidence could not be read/validated. WSL
host filesystem coverage remains a GAP without an approved host metric source.
"""
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlencode

NAMESPACES = ("monitoring", "personal-server")
PROXY = "/api/v1/namespaces/monitoring/services/http:personal-server-monitoring-prometheus:9090/proxy"
SELECTOR = '{namespace=~"monitoring|personal-server"}'
NODE_EXPRESSION = 'kube_node_status_condition{condition="DiskPressure",status="true"} == 1'
PVC_EXPRESSION = (
    '(min by (namespace, persistentvolumeclaim) (kubelet_volume_stats_available_bytes' + SELECTOR + ') '
    '/ max by (namespace, persistentvolumeclaim) (kubelet_volume_stats_capacity_bytes' + SELECTOR + ') < 0.15) '
    'and on (namespace, persistentvolumeclaim) '
    '(max by (namespace, persistentvolumeclaim) (kubelet_volume_stats_capacity_bytes' + SELECTOR + ') > 0)'
)
HOST_SELECTOR = '{host="windows",volume="C:"}'
HOST_PREFIX = "personal_server_host_disk_"
HOST_LOW_EXPRESSION = (
    '(personal_server_host_disk_available_bytes' + HOST_SELECTOR
    + ' / personal_server_host_disk_capacity_bytes' + HOST_SELECTOR + ' < 0.15) '
    'and on (host, volume) (personal_server_host_disk_collection_ok' + HOST_SELECTOR + ' == 1) '
    'and on (host, volume) (time() - personal_server_host_disk_sample_timestamp_seconds' + HOST_SELECTOR + ' <= 2100)'
)
HOST_STALE_EXPRESSION = (
    '(personal_server_host_disk_collection_ok' + HOST_SELECTOR + ' == 0) '
    'or (time() - personal_server_host_disk_sample_timestamp_seconds' + HOST_SELECTOR + ' > 2100) '
    'or absent(personal_server_host_disk_collection_ok' + HOST_SELECTOR + ')'
)


def read_json(arguments):
    result = subprocess.run(
        ["sudo", "-n", "k3s", "kubectl", "--request-timeout=20s", *arguments],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25, check=True,
    )
    return json.loads(result.stdout)


def api(path, **parameters):
    suffix = "?" + urlencode(parameters) if parameters else ""
    response = read_json(["get", "--raw", PROXY + "/api/v1/" + path + suffix])
    if response.get("status") != "success":
        raise ValueError("prometheus_api_failed")
    return response["data"]


def vector(expression, labels):
    data = api("query", query=expression)
    if data.get("resultType") != "vector" or not isinstance(data.get("result"), list):
        raise ValueError("invalid_vector")
    values = {}
    for sample in data["result"]:
        metric = sample["metric"]
        key = tuple(metric[label] for label in labels)
        value = float(sample["value"][1])
        if not math.isfinite(value) or value < 0:
            raise ValueError("invalid_sample")
        values.setdefault(key, []).append(value)
    return values


def rule_status(rules, name, expression, duration):
    matches = [rule for rule in rules if rule.get("name") == name]
    if len(matches) != 1:
        return "GAP"
    rule = matches[0]
    # Verify the loaded rule and routing label, rather than merely its name.
    expected_labels = {"sre_telegram": "true", "severity": "warning"}
    return "PASS" if (
        rule.get("health") == "ok"
        and isinstance(rule.get("query"), str)
        and isinstance(rule.get("labels"), dict)
        and "".join(rule["query"].split()) == "".join(expression.split())
        and rule.get("duration") == duration
        and all(rule.get("labels", {}).get(key) == value for key, value in expected_labels.items())
    ) else "GAP"


def host_disk_status(rules):
    """Require the dedicated private scrape and fresh, identified source data."""
    fields = ("collection_ok", "sample_timestamp_seconds", "capacity_bytes", "available_bytes")
    measurements = {field: vector(HOST_PREFIX + field + HOST_SELECTOR, ("host", "volume"))
                    for field in fields}
    key = ("windows", "C:")
    exact = all(set(data) == {key} and len(data[key]) == 1 for data in measurements.values())
    valid = False
    low = 0
    if exact:
        values = {field: data[key][0] for field, data in measurements.items()}
        age = datetime.now(timezone.utc).timestamp() - values["sample_timestamp_seconds"]
        valid = (values["collection_ok"] == 1 and -60 <= age <= 2100
                 and 0 <= values["available_bytes"] <= values["capacity_bytes"]
                 and values["capacity_bytes"] > 0)
        low = int(valid and values["available_bytes"] / values["capacity_bytes"] < 0.15)
    targets = [t for t in api("targets", state="active")["activeTargets"]
               if t.get("scrapePool") == "serviceMonitor/monitoring/windows-host-disk/0"]
    scrape = (len(targets) == 1 and targets[0].get("health") == "up"
              and targets[0].get("labels", {}).get("service") == "compose-system-agent"
              and targets[0].get("labels", {}).get("namespace") == "personal-server")
    low_rule = rule_status(rules, "HostDiskSpaceLow", HOST_LOW_EXPRESSION, 600)
    stale_rule = rule_status(rules, "HostDiskMetricsStale", HOST_STALE_EXPRESSION, 300)
    covered = valid and scrape and low_rule == stale_rule == "PASS"
    return {"coverage": "PASS" if covered else "GAP", "metrics": "PASS" if valid else "GAP",
            "scrape": "PASS" if scrape else "GAP", "low_space": low,
            "rules": {"space_low": low_rule, "source_stale": stale_rule},
            "scope": "windows_c_volume", "wsl_filesystem": "separate_node_and_pvc_signals"}


def audit():
    expected_pvcs = set()
    for namespace in NAMESPACES:
        for item in read_json(["-n", namespace, "get", "pvc", "-o", "json"])["items"]:
            if item.get("status", {}).get("phase") == "Bound":
                expected_pvcs.add((namespace, item["metadata"]["name"]))
    expected_nodes = {(item["metadata"]["name"],) for item in read_json(["get", "nodes", "-o", "json"])["items"]}
    rule_data = api("rules", type="alert")
    rules = [rule for group in rule_data["groups"] for rule in group["rules"] if rule.get("type") == "alerting"]
    available = vector("kubelet_volume_stats_available_bytes" + SELECTOR, ("namespace", "persistentvolumeclaim"))
    capacity = vector("kubelet_volume_stats_capacity_bytes" + SELECTOR, ("namespace", "persistentvolumeclaim"))
    pressure = vector('kube_node_status_condition{condition="DiskPressure",status="true"}', ("node",))
    # Windows C: has its own identified source; WSL node/PVC signals are separate.
    host = host_disk_status(rules)
    covered = {key for key in expected_pvcs if key in available and key in capacity
               and max(capacity[key]) > 0 and min(available[key]) <= max(capacity[key])}
    low = sum(min(available[key]) / max(capacity[key]) < 0.15 for key in covered)
    node_covered = {key for key in expected_nodes if key in pressure and all(value in (0, 1) for value in pressure[key])}
    disk_pressure = sum(max(pressure[key]) == 1 for key in node_covered)
    pvc_metrics = "PASS" if expected_pvcs and covered == expected_pvcs else "GAP"
    node_metrics = "PASS" if expected_nodes and node_covered == expected_nodes else "GAP"
    complete = (pvc_metrics == node_metrics == host["coverage"] == "PASS"
                and rule_status(rules, "PVCSpaceLow", PVC_EXPRESSION, 600) == "PASS"
                and rule_status(rules, "NodeDiskPressure", NODE_EXPRESSION, 300) == "PASS")
    status = "GAP" if not complete else ("WARNING" if low or disk_pressure or host["low_space"] else "PASS")
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "pvc": {"bound": len(expected_pvcs), "covered": len(covered), "low_space": low,
                "metrics": pvc_metrics, "rule": rule_status(rules, "PVCSpaceLow", PVC_EXPRESSION, 600)},
        "node": {"expected": len(expected_nodes), "covered": len(node_covered), "disk_pressure": disk_pressure,
                 "metrics": node_metrics, "rule": rule_status(rules, "NodeDiskPressure", NODE_EXPRESSION, 300)},
        "host_filesystem": host,
        "notification_delivery": "NOT_TESTED",
        "monitoring_disk_audit": status,
    }


def main():
    if len(sys.argv) != 1:
        print("usage: monitoring-disk-audit.py", file=sys.stderr)
        return 2
    try:
        report = audit()
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        # Command output/metric labels can include internal addresses or paths.
        print(json.dumps({"monitoring_disk_audit": "FAIL", "reason": "evidence_unavailable_or_invalid"}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["monitoring_disk_audit"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
