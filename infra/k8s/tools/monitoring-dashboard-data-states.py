#!/usr/bin/env python3
"""Render reviewed N100 dashboard overrides without changing metric collection.

Only build_plan emits payloads, in memory. The CLI prints counts and hashes only.
The deployment caller must use UID/RV/data-value CAS and retain exact rollback bytes.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parents[1] / "monitoring/dashboard-data-states.json"
POLICY = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
if POLICY["schema_version"] != 1 or POLICY["namespace"] != "monitoring":
    raise ValueError("invalid_dashboard_policy")
TARGETS = POLICY["targets"]
NOTE_ID = 9000
DEFAULT_NAMESPACE = "personal-server"
MULTI = "b59e6c9f2fcbe2e16d77fc492374cc4f"
KUBELET = "3138fa155d5915769fbded898ac09fd9"
NETWORK = "ff635a025bcfea7bc3dd4f508990a3e9"
CORE_DNS = "vkQ0UHxik"
API_SERVER = "09ec8aa1e996d6ffcd6817bbaff4db1b"
CONTROL = {
    "c2f4e12cdf69feb95caa41a5a1b423d9": "etcd",
    "72e0e05bef5099e5f049b05fdc429ed4": "Controller Manager",
    "632e265de029684c40b21cb76bca4f94": "Proxy",
    "2e6b6a3b4bddf1427b3a55aa1311c656": "Scheduler",
}
STORAGE_ERROR_EXPR = (
    'sum(rate(storage_operation_duration_seconds_count{cluster="$cluster",'
    'job="kubelet",metrics_path="/metrics",instance=~"$instance",'
    'status=~"fail-.+"}[$__rate_interval])) '
    'by (instance, operation_name, volume_plugin)'
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def document_sha(document: dict) -> str:
    return sha(json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def panels(dashboard: dict):
    for panel in dashboard.get("panels", []):
        yield panel
        yield from panels(panel)


def by_id(dashboard: dict, panel_id: int) -> dict:
    matches = [p for p in panels(dashboard) if p.get("id") == panel_id]
    require(len(matches) == 1, "panel_identity_changed")
    return matches[0]


def text_panel(panel: dict, title: str, content: str) -> None:
    identity = {key: panel[key] for key in ("id", "gridPos")}
    panel.clear()
    panel.update(identity, type="text", title=title,
                 options={"mode": "markdown", "content": content})


def display_cluster(expr: str) -> str:
    # Adds a presentation label only; never fabricates the actual cluster label.
    return ('label_replace(label_replace((' + expr + '),'
            '"display_cluster","$1","cluster","(.+)"),'
            '"display_cluster","N100","cluster","^$")')


def multi_table(panel: dict, memory: bool) -> None:
    targets = panel.get("targets", [])
    require(len(targets) == 5, "multi_table_target_count_changed")
    if all('"display_cluster"' in t.get("expr", "") for t in targets):
        return
    require(all("by (cluster)" in t.get("expr", "") for t in targets),
            "multi_table_query_shape_changed")
    for ref, target in zip("ABCDE", targets):
        target.update(expr=display_cluster(target["expr"]), refId=ref,
                      format="table", instant=True, range=False)
    names = ["Memory Usage", "Memory Requests", "Memory Requests %", "Memory Limits", "Memory Limits %"] if memory else ["CPU Usage", "CPU Requests", "CPU Requests %", "CPU Limits", "CPU Limits %"]
    panel["transformations"] = [
        {"id": "joinByField", "options": {"byField": "display_cluster", "mode": "outerTabular"}},
        {"id": "organize", "options": {
            "excludeByName": {"Time": True, "cluster": True, **{"Time " + str(i): True for i in range(1, 6)}},
            "indexByName": {"display_cluster": 0, **{"Value #" + ref: i for i, ref in enumerate("ABCDE", 1)}},
            "renameByName": {"display_cluster": "Cluster", **{"Value #" + ref: name for ref, name in zip("ABCDE", names)}},
        }},
    ]
    overrides = panel.setdefault("fieldConfig", {}).setdefault("overrides", [])
    # Preserve number formatting, replace only links that used the absent cluster field.
    for override in overrides:
        override["properties"] = [v for v in override.get("properties", []) if v.get("id") != "links"]
    overrides.append({"matcher": {"id": "byName", "options": "Cluster"}, "properties": [
        {"id": "links", "value": [{"title": "단일 Cluster 상세", "url": "/d/efa86fd1d0c121a26444b636a3f509a8?var-cluster=", "targetBlank": False}]}]})
    panel["description"] = "표시용 display_cluster로 결합함. 실제 cluster 라벨은 변경하지 않으며 N100 링크는 빈 cluster 필터를 사용함."


def add_notice(dashboard: dict, content: str) -> None:
    matches = [p for p in panels(dashboard) if p.get("id") == NOTE_ID]
    if matches:
        require(len(matches) == 1 and matches[0].get("type") == "text"
                and matches[0].get("title") == "데이터가 비어 있을 때", "notice_id_collision")
        matches[0]["options"] = {"mode": "markdown", "content": content}
        matches[0]["description"] = content
        return
    for panel in dashboard.get("panels", []):
        panel["gridPos"]["y"] += 4
    dashboard["panels"].insert(0, {
        "id": NOTE_ID, "type": "text", "title": "데이터가 비어 있을 때",
        "description": content,
        "gridPos": {"h": 4, "w": 24, "x": 0, "y": 0},
        "options": {"mode": "markdown", "content": content},
    })


def transform_dashboard(original: dict) -> dict:
    dashboard = copy.deepcopy(original)
    uid = dashboard.get("uid")
    require(uid in {x["dashboard_uid"] for x in TARGETS.values()}, "dashboard_not_allowlisted")
    variables = dashboard.get("templating", {}).get("list", [])
    namespace = next((v for v in variables if v.get("name") == "namespace"), None)
    note = ""
    if namespace:
        namespace["current"] = {"text": DEFAULT_NAMESPACE, "value": DEFAULT_NAMESPACE}
        namespace["options"] = []
        namespace["refresh"] = 1
        for variable in variables:
            if variable.get("name") in ("pod", "workload"):
                variable["current"] = {"text": "", "value": ""}
                variable["options"] = []
                variable["refresh"] = 1
            if variable.get("name") == "type":
                variable["current"] = {"text": "deployment", "value": "deployment"}
        note = "기본 namespace는 앱이 있는 **personal-server**입니다. 상단 필터로 다른 namespace·Pod·Workload를 선택할 수 있습니다. 선택한 대상이 없으면 값이 비며, 수집 오류나 0을 뜻하지 않습니다."
        if any(p.get("title") == "CPU Throttling" for p in panels(dashboard)):
            note += " **CPU Throttling**은 CPU 제한과 CFS 표본이 있는 컨테이너에서 계산합니다. CPU 제한이 없는 Pod는 해당 값이 비어 있을 수 있습니다."
    if uid in CONTROL:
        note = f"**현재 수집 대상 아님 — {CONTROL[uid]}**. 이 N100 구성은 해당 전용 지표를 수집하지 않습니다. 아래 빈 값으로 구성요소 장애나 정상값 0을 판단하지 않습니다. 앱·노드의 상태는 단일 Cluster 화면과 실제 수집 상태를 함께 확인하세요."
    elif uid == KUBELET:
        config = by_id(dashboard, 6)
        if config.get("type") != "text":
            require("kubelet_node_config_error" in json.dumps(config.get("targets")), "config_error_query_changed")
        text_panel(config, "구성 오류 지표: 미지원", "현재 버전은 구형 구성 오류 지표를 제공하지 않습니다. 대체 지표 미확인으로 값을 표시하지 않으며, 오류 0을 뜻하지 않습니다.")
        storage = by_id(dashboard, 13)
        require(len(storage.get("targets", [])) == 1, "storage_query_count_changed")
        require("storage_operation_errors_total" in storage["targets"][0]["expr"] or storage["targets"][0]["expr"] == STORAGE_ERROR_EXPR, "storage_query_changed")
        storage["targets"][0]["expr"] = STORAGE_ERROR_EXPR
        storage["description"] = "현재 storage duration histogram count의 fail-* 상태만 사용함. 실패 상태 표본이 없거나 작업 계측이 없으면 값이 비므로 작업 지표·수집 상태를 함께 확인함."
        note = "구형 구성 오류 패널은 **미지원 안내**로 바꿨습니다. Storage Operation Error Rate는 현재 지표의 **fail-* 상태**를 사용합니다. 실패 표본이 없는 경우와 작업 지표 자체가 없는 경우는 다르며, 빈 값을 0으로 바꾸지 않습니다."
    elif uid == MULTI:
        multi_table(by_id(dashboard, 8), False)
        multi_table(by_id(dashboard, 10), True)
        note = "이 환경은 단일 N100이며 실제 **cluster 라벨이 없습니다**. 표의 N100은 단일 환경의 표시 이름이며, 상세 링크는 단일 Cluster 화면으로 연결합니다. 다른 클러스터가 추가되면 표와 필터를 다시 검토해야 합니다."
    elif uid == NETWORK:
        for panel_id, title in ((12, "TCP 재전송"), (13, "TCP SYN 재전송")):
            panel = by_id(dashboard, panel_id)
            if panel.get("type") != "text":
                require("node_netstat_" in json.dumps(panel.get("targets")), "tcp_query_changed")
            text_panel(panel, title + ": 수집 범위 밖", "현재 노드 수집기는 CPU·meminfo만 수집하므로 이 TCP 지표는 제공하지 않습니다. 컨테이너 네트워크 값으로 WSL 노드 전체 값을 대체하지 않습니다.")
        note = "현재 노드 수집 범위는 **CPU·메모리**입니다. TCP 재전송 두 항목은 수집 범위 밖으로 표시합니다. 아래 Pod 네트워크 지표와 WSL 노드 전체 TCP 지표는 구분합니다."
    elif uid == CORE_DNS:
        note = "TCP 크기 분포는 선택한 기간에 **TCP 요청 표본**이 있어야 계산됩니다. UDP 요청만 있다면 TCP 두 그래프가 비어 있을 수 있습니다. CoreDNS 수집 상태·요청 수를 함께 확인하세요. 조회 오류를 표본 없음으로 해석하지 않습니다."
    elif uid == API_SERVER:
        note = "Write SLI - Errors는 **5xx 쓰기 오류 표본**이 없으면 비어 있을 수 있습니다. 쓰기 요청 수와 수집 상태를 함께 확인하세요. 빈 값만으로 오류 0 또는 계측 누락을 단정하지 않습니다."
    elif uid in ("portal-http-observability", "portal-search-observability"):
        note = "검색 비율·지연은 해당 조회 기간에 **실제 검색 표본**이 있어야 계산됩니다. 무트래픽과 수집·계측 누락을 구분하려면 검색 요청 수, 계측 누락 Ready Pod 및 수집 상태를 함께 확인하세요. 빈 값을 성공률 100%나 지연 0으로 표시하지 않습니다."
        for panel in panels(dashboard):
            if panel.get("type") == "stat" and ("검색" in panel.get("title", "")):
                panel.setdefault("fieldConfig", {}).setdefault("defaults", {})["noValue"] = "검색 표본 없음 · 계측 상태 확인"
                for target in panel.get("targets", []):
                    target.update(instant=True, range=False)
                panel.setdefault("options", {}).setdefault("reduceOptions", {})["calcs"] = ["last"]
    require(bool(note), "missing_dashboard_notice")
    add_notice(dashboard, note)
    return dashboard


def build_plan(configmaps: list[dict], *, helm_rendered: bool = False) -> list[dict]:
    require(isinstance(configmaps, list), "configmap_list_required")
    targets = {name: spec for name, spec in TARGETS.items() if not helm_rendered or spec["helm_owned"]}
    selected = {cm["metadata"]["name"]: cm for cm in configmaps if cm.get("metadata", {}).get("name") in targets}
    require(len(selected) == len(targets), "target_configmap_missing")
    require(sum(cm.get("metadata", {}).get("name") in targets for cm in configmaps) == len(targets), "duplicate_configmap")
    seen = set()
    for cm in configmaps:
        for value in cm.get("data", {}).values():
            try:
                document = json.loads(value)
            except (ValueError, TypeError):
                continue
            if isinstance(document, dict) and document.get("uid") in {v["dashboard_uid"] for v in TARGETS.values()}:
                require(document["uid"] not in seen, "duplicate_dashboard_uid")
                seen.add(document["uid"])
    plan = []
    for name, spec in targets.items():
        cm = selected[name]
        metadata = cm["metadata"]
        require(metadata.get("namespace") == "monitoring" and metadata.get("labels", {}).get("grafana_dashboard") == "1", "configmap_profile_changed")
        owner = metadata.get("annotations", {}).get("meta.helm.sh/release-name")
        if helm_rendered:
            require(cm.get("apiVersion") == "v1" and cm.get("kind") == "ConfigMap", "configmap_api_changed")
            require(owner in (None, "personal-server-monitoring"), "configmap_owner_changed")
            labels = metadata["labels"]
            require(all(labels.get(key) == expected for key, expected in {
                "app.kubernetes.io/managed-by": "Helm",
                "app.kubernetes.io/instance": "personal-server-monitoring",
                "release": "personal-server-monitoring",
                "chart": "kube-prometheus-stack-88.6.1",
            }.items()), "rendered_configmap_profile_changed")
        else:
            require((owner == "personal-server-monitoring") if spec["helm_owned"] else owner is None, "configmap_owner_changed")
        before = cm.get("data", {}).get(spec["key"])
        require(isinstance(before, str), "dashboard_key_missing")
        original = json.loads(before)
        require(original.get("uid") == spec["dashboard_uid"], "dashboard_uid_changed")
        after = transform_dashboard(original)
        if document_sha(original) == spec["after_sha256"]:
            require(after == original, "reviewed_after_not_idempotent")
            continue
        require(sha(before) == spec["source_sha256"], "unreviewed_dashboard_source")
        require(document_sha(after) == spec["after_sha256"], "renderer_policy_mismatch")
        plan.append({"name": name, "key": spec["key"], "dashboard_uid": spec["dashboard_uid"],
                     "before": before, "after": json.dumps(after, ensure_ascii=False, indent=2) + "\n"})
    return plan


def expected_checks(after: str) -> list[dict]:
    dashboard = json.loads(after)
    checks = [{"path": ["uid"], "expected": dashboard["uid"]}]
    def visit(items: list, prefix: list):
        for index, panel in enumerate(items):
            path = prefix + [index]
            for key in ("id", "type", "title", "gridPos", "description"):
                if key in panel:
                    checks.append({"path": path + [key], "expected": panel[key]})
            for ti, target in enumerate(panel.get("targets", [])):
                for key in ("expr", "refId", "format", "instant", "range"):
                    if key in target:
                        checks.append({"path": path + ["targets", ti, key], "expected": target[key]})
            if panel.get("type") == "text":
                checks.append({"path": path + ["options", "content"], "expected": panel["options"]["content"]})
            if panel.get("type") == "table" and dashboard["uid"] == MULTI:
                checks.append({"path": path + ["transformations"], "expected": panel["transformations"]})
                checks.append({"path": path + ["fieldConfig", "overrides"], "expected": panel["fieldConfig"]["overrides"]})
            if panel.get("type") == "stat" and "noValue" in panel.get("fieldConfig", {}).get("defaults", {}):
                checks.append({"path": path + ["fieldConfig", "defaults", "noValue"], "expected": panel["fieldConfig"]["defaults"]["noValue"]})
                if "calcs" in panel.get("options", {}).get("reduceOptions", {}):
                    checks.append({"path": path + ["options", "reduceOptions", "calcs"], "expected": panel["options"]["reduceOptions"]["calcs"]})
            visit(panel.get("panels", []), path + ["panels"])
    visit(dashboard["panels"], ["panels"])
    for i, variable in enumerate(dashboard.get("templating", {}).get("list", [])):
        if variable.get("name") in ("namespace", "type", "pod", "workload") and "current" in variable:
            checks.append({"path": ["templating", "list", i, "current", "value"], "expected": variable["current"].get("value")})
    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", required=True)
    parser.parse_args()
    source = json.load(sys.stdin)
    plan = build_plan(source.get("items", []) if isinstance(source, dict) else source)
    print(json.dumps({"changes": len(plan), "dashboards": [
        {"name": x["name"], "uid": x["dashboard_uid"], "after_sha256": sha(x["after"])} for x in plan]}))


if __name__ == "__main__":
    main()
