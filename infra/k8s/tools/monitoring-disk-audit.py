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
    # Read the signal, but do not infer physical-host identity/mount coverage from
    # an arbitrary exporter instance. WSL nodeExporter is intentionally disabled.
    host = vector('node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"}', ("instance", "mountpoint"))
    covered = {key for key in expected_pvcs if key in available and key in capacity
               and max(capacity[key]) > 0 and min(available[key]) <= max(capacity[key])}
    low = sum(min(available[key]) / max(capacity[key]) < 0.15 for key in covered)
    node_covered = {key for key in expected_nodes if key in pressure and all(value in (0, 1) for value in pressure[key])}
    disk_pressure = sum(max(pressure[key]) == 1 for key in node_covered)
    pvc_metrics = "PASS" if expected_pvcs and covered == expected_pvcs else "GAP"
    node_metrics = "PASS" if expected_nodes and node_covered == expected_nodes else "GAP"
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "pvc": {"bound": len(expected_pvcs), "covered": len(covered), "low_space": low,
                "metrics": pvc_metrics, "rule": rule_status(rules, "PVCSpaceLow", PVC_EXPRESSION, 600)},
        "node": {"expected": len(expected_nodes), "covered": len(node_covered), "disk_pressure": disk_pressure,
                 "metrics": node_metrics, "rule": rule_status(rules, "NodeDiskPressure", NODE_EXPRESSION, 300)},
        "host_filesystem": {"observed_series": len(host), "coverage": "GAP",
                            "reason": "host_identity_and_mount_coverage_not_validated"},
        "notification_delivery": "NOT_TESTED",
        "monitoring_disk_audit": "GAP",
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
    return 1  # Host disk coverage is not established by the current WSL stack.


if __name__ == "__main__":
    raise SystemExit(main())
