#!/usr/bin/env python3
"""Apply reviewed dashboard data to Helm's manifest stream, entirely in memory.

Only allowlisted ConfigMap data scalar spans are replaced. All other input bytes,
including Secrets, annotations, comments and document separators, are preserved.
No manifest content or parser exceptions may be printed on failure.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import yaml
from yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent
from yaml.nodes import MappingNode, ScalarNode

MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 48 * 1024 * 1024
MAX_NODES = 250_000
MAX_DEPTH = 64
MAX_DOCUMENTS = 2000
MAX_DASHBOARD_BYTES = 2 * 1024 * 1024
ERROR = "monitoring_dashboard_post_renderer=FAIL\n"

spec = importlib.util.spec_from_file_location(
    "monitoring_dashboard_data_states", Path(__file__).with_name("monitoring-dashboard-data-states.py"))
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


class StrictLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = set()
        for key, _ in node.value:
            # Kubernetes object maps require string keys; this also rejects YAML
            # merges, whose implicit overrides would make source spans ambiguous.
            if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str":
                raise ValueError("invalid_mapping_key")
            if key.value in keys:
                raise ValueError("duplicate_mapping_key")
            keys.add(key.value)
        return super().construct_mapping(node, deep=deep)


def field(node: MappingNode, name: str):
    matches = [value for key, value in node.value if key.value == name]
    if len(matches) != 1:
        raise ValueError("missing_or_duplicate_field")
    return matches[0]


def render(raw: bytes) -> bytes:
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ValueError("invalid_input_size")
    source = raw.decode("utf-8")
    depth = count = 0
    for event in yaml.parse(source, Loader=StrictLoader):
        count += 1
        if isinstance(event, AliasEvent) or getattr(event, "anchor", None) is not None:
            raise ValueError("yaml_alias_or_anchor")
        if isinstance(event, CollectionStartEvent):
            depth += 1
        elif isinstance(event, CollectionEndEvent):
            depth -= 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError("yaml_limit_exceeded")

    documents, nodes = [], []
    loader = StrictLoader(source)
    try:
        while loader.check_data():
            node = loader.get_node()
            document = loader.construct_document(node)
            if not isinstance(document, dict) or not isinstance(node, MappingNode):
                raise ValueError("object_document_required")
            if not isinstance(document.get("apiVersion"), str) or not isinstance(document.get("kind"), str):
                raise ValueError("kubernetes_document_required")
            if not isinstance(document.get("metadata"), dict):
                raise ValueError("metadata_required")
            documents.append(document)
            nodes.append(node)
            if len(documents) > MAX_DOCUMENTS:
                raise ValueError("document_limit_exceeded")
    finally:
        loader.dispose()

    configmaps = [document for document in documents if document["kind"] == "ConfigMap"]
    identities = set()
    for cm in configmaps:
        identity = (cm["metadata"].get("namespace"), cm["metadata"].get("name"))
        if identity in identities:
            raise ValueError("duplicate_configmap")
        identities.add(identity)
        if not isinstance(cm.get("data", {}), dict):
            raise ValueError("invalid_configmap_data")
        for value in cm.get("data", {}).values():
            if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_DASHBOARD_BYTES:
                raise ValueError("invalid_configmap_value")

    plan = renderer.build_plan(configmaps, helm_rendered=True)
    replacements = {(entry["name"], entry["key"]): entry["after"] for entry in plan}
    spans = []
    for document, node in zip(documents, nodes):
        if document["kind"] != "ConfigMap":
            continue
        name = document["metadata"].get("name")
        for (target, key), after in replacements.items():
            if target != name:
                continue
            value = field(field(node, "data"), key)
            if not isinstance(value, ScalarNode):
                raise ValueError("scalar_dashboard_required")
            start, end = value.start_mark.index, value.end_mark.index
            # Escape Unicode line separators too: YAML treats literal NEL/LS/PS
            # as line breaks even inside a JSON-compatible double quoted scalar.
            replacement = json.dumps(after, ensure_ascii=True)
            # Block scalars include their final newline in the source span.
            if source[start:end].endswith("\n"):
                replacement += "\n"
            spans.append((start, end, replacement))
    for start, end, replacement in sorted(spans, reverse=True):
        source = source[:start] + replacement + source[end:]
    result = source.encode("utf-8")
    if len(result) > MAX_OUTPUT_BYTES:
        raise ValueError("output_limit_exceeded")
    return result


def main() -> int:
    try:
        if len(sys.argv) != 1:
            raise ValueError("arguments_not_supported")
        result = render(sys.stdin.buffer.read(MAX_INPUT_BYTES + 1))
    except Exception:
        sys.stderr.write(ERROR)
        return 2
    sys.stdout.buffer.write(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
