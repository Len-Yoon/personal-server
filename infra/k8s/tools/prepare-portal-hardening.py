#!/usr/bin/env python3
"""Prepare Portal rootfs hardening or its exact inverse JSON Patch; never apply.

Supply a fresh Deployment snapshot and isolated same-image read-only startup/write
proof. Every existing writable mount and /tmp must have create/write/delete proof
using synthetic data. Preserve image, data/PVC, env, resources, probes and writer.
Rollback requires a fresh snapshot plus the original, trusted rollback record.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import PurePosixPath
import re

POD = '/spec/template/spec'
CONTAINER = POD + '/containers/0'
TMP_NAME = 'portal-hardening-tmp'
TMP_VOLUME = {'name': TMP_NAME, 'emptyDir': {}}
TMP_MOUNT = {'name': TMP_NAME, 'mountPath': '/tmp'}
SECCOMP = {'type': 'RuntimeDefault'}
SECURITY = {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}


def require(condition, code):
    if not condition:
        raise ValueError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def snapshot_parts(snapshot):
    meta = snapshot['metadata']; spec = snapshot['spec']; pod = spec['template']['spec']
    require(snapshot.get('kind') == 'Deployment' and meta.get('name') == 'portal-web' and meta.get('namespace') == 'personal-server', 'portal_deployment_required')
    require(all(isinstance(meta.get(key), str) and meta[key] for key in ('uid', 'resourceVersion')), 'live_snapshot_required')
    require(type(spec.get('replicas')) is int and spec['replicas'] == 1 and spec.get('strategy', {}).get('type') == 'Recreate', 'single_writer_recreate_required')
    require(len(pod['containers']) == 1 and pod['containers'][0]['name'] == 'portal-web', 'single_portal_container_required')
    container = pod['containers'][0]
    require(re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}', container.get('image', '')) is not None, 'immutable_image_required')
    require(pod.get('automountServiceAccountToken') is False, 'token_automount_must_remain_disabled')
    require(not any(pod.get(key) for key in ('hostNetwork', 'hostPID', 'hostIPC', 'shareProcessNamespace', 'initContainers', 'ephemeralContainers')), 'host_or_extra_container_review_required')
    require(not any(port.get('hostPort', 0) for port in container.get('ports', [])), 'host_port_review_required')
    for volume in pod.get('volumes', []):
        require('hostPath' not in volume and not any('serviceAccountToken' in source for source in volume.get('projected', {}).get('sources', [])), 'host_or_token_volume_forbidden')
    return meta, pod, container


def validate_security(pod, container):
    security = container.get('securityContext', {})
    pod_security = pod.get('securityContext', {})
    require(isinstance(security, dict) and isinstance(pod_security, dict), 'security_objects_required')
    require(pod_security.get('runAsNonRoot') is True and pod_security.get('runAsUser') == 10001, 'existing_nonroot_profile_required')
    require(security.get('runAsNonRoot', True) is True and security.get('runAsUser', 10001) == 10001 and security.get('privileged', False) is False and security.get('procMount', 'Default') == 'Default', 'effective_nonroot_nonprivileged_required')
    for profile in (pod_security, security):
        require('seccompProfile' not in profile or profile['seccompProfile'] == SECCOMP, 'conflicting_seccomp_profile')
    for key, value in SECURITY.items():
        require(key not in security or security[key] == value, 'conflicting_container_security')
    return pod_security, security


def validate_paths(pod, container):
    volumes = pod.get('volumes', [])
    mounts = container.get('volumeMounts', [])
    require(isinstance(volumes, list) and isinstance(mounts, list), 'volume_lists_required')
    names = [volume['name'] for volume in volumes]
    require(len(names) == len(set(names)) and TMP_NAME not in names, 'tmp_volume_name_collision')
    require(all(mount.get('name') != TMP_NAME for mount in mounts), 'tmp_mount_name_collision')
    mapping = {volume['name']: volume for volume in volumes}
    paths = []
    writable = set()
    for mount in mounts:
        path = mount['mountPath']
        require(isinstance(path, str) and path.startswith('/') and str(PurePosixPath(path)) == path and '..' not in PurePosixPath(path).parts, 'normalized_absolute_mount_required')
        require(path != '/' and path != '/tmp' and not path.startswith('/tmp/'), 'tmp_mount_path_collision')
        require(mount.get('mountPropagation', 'None') == 'None' and mount['name'] in mapping, 'safe_existing_mount_required')
        paths.append(path)
        if not mount.get('readOnly', False):
            # Avoid making mutable Secret/configMap or unknown storage appear to
            # be a valid data write path during a rootfs transition.
            require('persistentVolumeClaim' in mapping[mount['name']], 'writable_pvc_mount_required')
            writable.add(path)
    require(len(paths) == len(set(paths)) and writable, 'unique_writable_data_mounts_required')
    return writable | {'/tmp'}


def validate_evidence(evidence, meta, container, paths):
    require(isinstance(evidence, dict) and evidence.get('deployment_uid') == meta['uid'] and evidence.get('resource_version') == meta['resourceVersion'] and evidence.get('image') == container['image'], 'snapshot_bound_evidence_required')
    observed = datetime.fromisoformat(evidence['observed_at'].replace('Z', '+00:00'))
    require(observed.tzinfo is not None and 0 <= (datetime.now(timezone.utc) - observed).total_seconds() <= 86400, 'fresh_timezone_aware_evidence_required')
    isolated = evidence['isolated']
    require(isolated.get('image') == container['image'] and isolated.get('platform') == 'linux/amd64' and type(isolated.get('uid')) is int and isolated['uid'] == 10001, 'same_image_isolated_nonroot_proof_required')
    require(all(isolated.get(key) is True for key in ('read_only_rootfs', 'root_write_denied', 'no_production_data', 'write_paths_complete', 'configured_write_paths_within_mounts', 'write_path_sources_reviewed')), 'isolated_readonly_and_complete_write_proof_required')
    require(type(isolated.get('health_status')) is int and isolated['health_status'] == 200 and type(isolated.get('ready_status')) is int and isolated['ready_status'] == 200, 'isolated_health_ready_200_required')
    writes = isolated['writable_paths']
    require(isinstance(writes, dict) and set(writes) == paths and all(isinstance(proof, dict) and proof.get('create_write_delete') is True for proof in writes.values()), 'all_writable_paths_must_be_proven')


def guards(meta, container):
    # resourceVersion protects the complete Deployment without echoing env or
    # the original pod spec, which may contain runtime credentials.
    return [{'op': 'test', 'path': '/metadata/uid', 'value': meta['uid']}, {'op': 'test', 'path': '/metadata/resourceVersion', 'value': meta['resourceVersion']}, {'op': 'test', 'path': CONTAINER + '/name', 'value': 'portal-web'}, {'op': 'test', 'path': CONTAINER + '/image', 'value': container['image']}]


def _parent(document, path):
    keys = path.strip('/').split('/')
    parent = document
    for key in keys[:-1]:
        parent = parent[int(key)] if isinstance(parent, list) else parent[key]
    return parent, int(keys[-1]) if isinstance(parent, list) else keys[-1]


def prepare(snapshot, evidence):
    meta, pod, container = snapshot_parts(snapshot)
    pod_security, security = validate_security(pod, container)
    paths = validate_paths(pod, container)
    validate_evidence(evidence, meta, container, paths)
    additions = []
    if 'seccompProfile' not in pod_security:
        additions.append({'path': POD + '/securityContext/seccompProfile', 'value': deepcopy(SECCOMP)})
    if 'securityContext' not in container:
        additions.append({'path': CONTAINER + '/securityContext', 'value': deepcopy(SECURITY)})
    else:
        for key, value in SECURITY.items():
            if key not in security:
                additions.append({'path': CONTAINER + '/securityContext/' + key, 'value': deepcopy(value)})
    additions.extend([{'path': POD + '/volumes/' + str(len(pod['volumes'])), 'value': deepcopy(TMP_VOLUME)}, {'path': CONTAINER + '/volumeMounts/' + str(len(container['volumeMounts'])), 'value': deepcopy(TMP_MOUNT)}])
    record = {'schema_version': 1, 'deployment_uid': meta['uid'], 'image': container['image'], 'original_spec_sha256': digest(snapshot['spec']), 'added_fields': deepcopy(additions)}
    return {'deployment': 'portal-web', 'namespace': 'personal-server', 'patch_type': 'json', 'patch': guards(meta, container) + [{'op': 'add', **item} for item in additions], 'rollback_record': record, 'evidence_sha256': digest(evidence), 'approval_required': True, 'preconditions': ['same reviewed image and fresh Deployment snapshot', 'no active backup and single writer; hold existing sequence lock during rollout', 'verify data/protected resource hashes and internal/external health before/after', 'disk emptyDir has no added quota; review node storage headroom and verify configured maximum ZIP plus cleanup']}


def permitted_addition(item):
    require(isinstance(item, dict) and set(item) == {'path', 'value'}, 'invalid_rollback_addition')
    path = item['path']; value = item['value']
    expected = {POD + '/securityContext/seccompProfile': SECCOMP, CONTAINER + '/securityContext': SECURITY, **{CONTAINER + '/securityContext/' + key: val for key, val in SECURITY.items()}}
    if path in expected:
        require(value == expected[path], 'untrusted_rollback_security_value')
    elif isinstance(path, str) and re.fullmatch(re.escape(POD) + r'/volumes/(0|[1-9][0-9]*)', path):
        require(value == TMP_VOLUME, 'untrusted_rollback_volume')
    elif isinstance(path, str) and re.fullmatch(re.escape(CONTAINER) + r'/volumeMounts/(0|[1-9][0-9]*)', path):
        require(value == TMP_MOUNT, 'untrusted_rollback_mount')
    else:
        raise ValueError('untrusted_rollback_path')


def prepare_rollback(snapshot, record):
    meta, _, container = snapshot_parts(snapshot)
    require(record.get('schema_version') == 1 and record.get('deployment_uid') == meta['uid'] and record.get('image') == container['image'], 'rollback_identity_mismatch')
    additions = record['added_fields']
    require(isinstance(additions, list) and additions, 'rollback_additions_required')
    for item in additions:
        permitted_addition(item)
    paths = [item['path'] for item in additions]
    require(len(paths) == len(set(paths)), 'duplicate_rollback_addition')
    require(not (CONTAINER + '/securityContext' in paths and any(path.startswith(CONTAINER + '/securityContext/') for path in paths)), 'overlapping_rollback_additions')
    restored = deepcopy(snapshot)
    patch = guards(meta, container)
    for item in reversed(additions):
        parent, key = _parent(restored, item['path'])
        require(parent[key] == item['value'], 'managed_hardening_value_changed')
        patch.append({'op': 'test', 'path': item['path'], 'value': deepcopy(item['value'])})
        patch.append({'op': 'remove', 'path': item['path']})
        if isinstance(parent, list):
            parent.pop(key)
        else:
            del parent[key]
    require(digest(restored['spec']) == record.get('original_spec_sha256'), 'unmanaged_spec_changed')
    return {'deployment': 'portal-web', 'namespace': 'personal-server', 'patch_type': 'json', 'patch': patch, 'approval_required': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--evidence')
    mode.add_argument('--rollback-record')
    mode.add_argument('--describe-input', action='store_true')
    args = parser.parse_args()
    if args.describe_input:
        print(json.dumps({'deployment_uid': 'REQUIRED', 'resource_version': 'REQUIRED', 'image': 'REQUIRED_IMMUTABLE_DIGEST', 'observed_at': 'REQUIRED_UTC_ISO8601', 'isolated': {'image': 'SAME_IMMUTABLE_DIGEST', 'platform': 'linux/amd64', 'read_only_rootfs': False, 'root_write_denied': False, 'uid': 10001, 'health_status': None, 'ready_status': None, 'no_production_data': False, 'write_paths_complete': False, 'configured_write_paths_within_mounts': False, 'write_path_sources_reviewed': False, 'writable_paths': {'EVERY_EXISTING_WRITABLE_PVC_MOUNT_AND_/tmp': {'create_write_delete': False}}}}, indent=2))
        return
    if not args.snapshot or not (args.evidence or args.rollback_record):
        parser.error('--snapshot and --evidence or --rollback-record are required')
    try:
        with open(args.snapshot) as source:
            snapshot = json.load(source)
        with open(args.evidence or args.rollback_record) as source:
            proof = json.load(source)
        result = prepare(snapshot, proof) if args.evidence else prepare_rollback(snapshot, proof)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError):
        parser.exit(1, 'portal_hardening_prepare=FAIL; verify snapshot and proof contract\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
