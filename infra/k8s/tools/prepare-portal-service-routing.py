#!/usr/bin/env python3
"""Prepare six Portal alias-to-Service URL replacements or their exact inverse.

Offline only. Require fresh snapshot-bound, secret-free probes from the Portal
pod: each old alias failed and every direct search/health URL returned HTTP 200.
Never include unrelated env values in output; never apply or restore whole spec.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re

CONTAINER = '/spec/template/spec/containers/0'
ROUTES = {
    name: (f'http://{old}:{port}/{path}', f'http://{new}:{port}/{path}')
    for prefix, old, new, port in [('NEWS', 'compose-crawler', 'crawler-worker', 8001), ('YOUTUBE', 'compose-youtube', 'youtube-memo', 8002), ('BOOKS', 'compose-book', 'book-memo', 8003)]
    for suffix, path in [('SEARCH', 'api/search'), ('HEALTH', 'health')]
    for name in [prefix + '_' + suffix + '_URL']
}

def require(condition, code):
    if not condition:
        raise ValueError(code)

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def snapshot_parts(snapshot):
    meta = snapshot['metadata']; spec = snapshot['spec']; pod = spec['template']['spec']
    require(snapshot.get('kind') == 'Deployment' and meta.get('name') == 'portal-web' and meta.get('namespace') == 'personal-server', 'portal_deployment_required')
    require(all(isinstance(meta.get(key), str) and meta[key] for key in ('uid', 'resourceVersion')), 'live_identity_required')
    require(type(spec.get('replicas')) is int and spec['replicas'] == 1 and spec.get('strategy', {}).get('type') == 'Recreate', 'single_writer_recreate_required')
    containers = pod['containers']
    require(isinstance(containers, list) and len(containers) == 1 and containers[0].get('name') == 'portal-web', 'single_portal_container_required')
    container = containers[0]
    require(re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}', container.get('image', '')) is not None, 'immutable_image_required')
    env = container['env']; require(isinstance(env, list), 'literal_env_list_required')
    names = [entry['name'] for entry in env]
    require(len(names) == len(set(names)), 'duplicate_env_name')
    targets = {name: (index, env[index]) for index, name in enumerate(names) if name in ROUTES}
    require(set(targets) == set(ROUTES) and all(set(entry) == {'name', 'value'} and isinstance(entry['value'], str) for index, entry in targets.values()), 'exact_six_literal_routes_required')
    return meta, container, targets

def guards(meta, container):
    return [{'op': 'test', 'path': '/metadata/uid', 'value': meta['uid']}, {'op': 'test', 'path': '/metadata/resourceVersion', 'value': meta['resourceVersion']}, {'op': 'test', 'path': CONTAINER + '/name', 'value': 'portal-web'}, {'op': 'test', 'path': CONTAINER + '/image', 'value': container['image']}]

def validate_evidence(snapshot, evidence, meta, container):
    require(evidence.get('deployment_uid') == meta['uid'] and evidence.get('resource_version') == meta['resourceVersion'] and evidence.get('image') == container['image'] and evidence.get('spec_sha256') == digest(snapshot['spec']), 'snapshot_bound_evidence_required')
    observed = datetime.fromisoformat(evidence['observed_at'].replace('Z', '+00:00'))
    require(observed.tzinfo is not None and 0 <= (datetime.now(timezone.utc) - observed).total_seconds() <= 86400, 'fresh_timezone_aware_evidence_required')
    require(evidence.get('single_writer_verified') is True and evidence.get('no_secret_access') is True and evidence.get('probe_origin') == 'portal-web-pod', 'single_writer_secret_free_pod_proof_required')
    probes = evidence['routes']; require(isinstance(probes, dict) and set(probes) == set(ROUTES), 'six_route_probes_required')
    for name, (old, new) in ROUTES.items():
        proof = probes[name]
        require(proof.get('old_url') == old and proof.get('direct_url') == new and proof.get('old_alias_failed') is True and type(proof.get('direct_status')) is int and proof['direct_status'] == 200, 'old_alias_failure_and_direct_200_required')

def change_patch(meta, container, changes, inverse=False):
    patch = guards(meta, container)
    for item in changes:
        path = item['path']; base = path.removesuffix('/value')
        old, new = (item['new_value'], item['old_value']) if inverse else (item['old_value'], item['new_value'])
        patch.extend([{'op': 'test', 'path': base + '/name', 'value': item['name']}, {'op': 'test', 'path': path, 'value': old}, {'op': 'replace', 'path': path, 'value': new}])
    return patch

def prepare(snapshot, evidence):
    meta, container, targets = snapshot_parts(snapshot)
    validate_evidence(snapshot, evidence, meta, container)
    changes = []
    for name, (old, new) in ROUTES.items():
        index, entry = targets[name]; require(entry['value'] == old, 'exact_old_alias_required')
        changes.append({'name': name, 'path': CONTAINER + '/env/' + str(index) + '/value', 'old_value': old, 'new_value': new})
    record = {'schema_version': 1, 'deployment_uid': meta['uid'], 'image': container['image'], 'original_spec_sha256': digest(snapshot['spec']), 'changed_fields': deepcopy(changes)}
    return {'deployment': 'portal-web', 'namespace': 'personal-server', 'patch_type': 'json', 'patch': change_patch(meta, container, changes), 'rollback_record': record, 'evidence_sha256': digest(evidence), 'approval_required': True, 'preconditions': ['hold existing sequence lock; no active backup and single writer', 'verify protected data/resource hashes and health before/after', 'combine with reviewed hardening patch only after validating the routed synthetic snapshot']}

def prepare_rollback(snapshot, record):
    meta, container, targets = snapshot_parts(snapshot)
    require(record.get('schema_version') == 1 and record.get('deployment_uid') == meta['uid'] and record.get('image') == container['image'], 'rollback_identity_mismatch')
    changes = record['changed_fields']
    require(isinstance(changes, list) and len(changes) == 6, 'six_rollback_changes_required')
    require(all(isinstance(item, dict) and set(item) == {'name', 'path', 'old_value', 'new_value'} for item in changes), 'invalid_rollback_record')
    require({item['name'] for item in changes} == set(ROUTES), 'exact_six_rollback_names_required')
    restored = deepcopy(snapshot)
    for item in changes:
        name = item['name']; index, entry = targets[name]; old, new = ROUTES[name]
        require(item['path'] == CONTAINER + '/env/' + str(index) + '/value' and item['old_value'] == old and item['new_value'] == new and entry['value'] == new, 'managed_route_or_record_changed')
        restored['spec']['template']['spec']['containers'][0]['env'][index]['value'] = old
    require(digest(restored['spec']) == record.get('original_spec_sha256'), 'unmanaged_spec_changed')
    return {'deployment': 'portal-web', 'namespace': 'personal-server', 'patch_type': 'json', 'patch': change_patch(meta, container, changes, inverse=True), 'approval_required': True}

def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--snapshot')
    mode = parser.add_mutually_exclusive_group(); mode.add_argument('--evidence'); mode.add_argument('--rollback-record'); mode.add_argument('--describe-input', action='store_true')
    args = parser.parse_args()
    if args.describe_input:
        print(json.dumps({'deployment_uid': 'REQUIRED', 'resource_version': 'REQUIRED', 'image': 'IMMUTABLE_DIGEST', 'spec_sha256': 'CANONICAL_SPEC_SHA256', 'observed_at': 'UTC_ISO8601', 'single_writer_verified': False, 'no_secret_access': False, 'probe_origin': 'portal-web-pod', 'routes': {name: {'old_url': old, 'direct_url': new, 'old_alias_failed': False, 'direct_status': None} for name, (old, new) in ROUTES.items()}}, indent=2)); return
    if not args.snapshot or not (args.evidence or args.rollback_record): parser.error('--snapshot and --evidence or --rollback-record are required')
    try:
        with open(args.snapshot) as source: snapshot = json.load(source)
        with open(args.evidence or args.rollback_record) as source: proof = json.load(source)
        result = prepare(snapshot, proof) if args.evidence else prepare_rollback(snapshot, proof)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError):
        parser.exit(1, 'portal_service_routing_prepare=FAIL; verify snapshot and proof contract\n')
    print(json.dumps(result, indent=2))

if __name__ == '__main__': main()
