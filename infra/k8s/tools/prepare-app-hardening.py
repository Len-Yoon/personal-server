#!/usr/bin/env python3
"""Prepare, never apply, image-bound hardening from reviewed measurements.

Usage: --snapshot deployment.json --evidence evidence.json
--describe-input prints an intentionally incomplete evidence template. Measurements
must cover representative load and include tmpfs usage in container memory. The
24-hour/288-sample minimum and 50% peak headroom are review gates, not a guarantee
against OOM; operators must also verify node capacity and representative workloads.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import re

APPS = {'portal-web', 'book-memo', 'youtube-memo', 'crawler-worker'}
IMAGE = re.compile(r'[^\s]+@sha256:[0-9a-f]{64}\Z')


def validate_snapshot(snapshot):
    meta = snapshot['metadata']
    name = meta['name']
    if snapshot.get('kind') != 'Deployment' or name not in APPS or meta.get('namespace') != 'personal-server':
        raise ValueError('unsupported_deployment')
    if not meta.get('uid') or not meta.get('resourceVersion'):
        raise ValueError('live_snapshot_required')
    pod = snapshot['spec']['template']['spec']
    if pod.get('hostNetwork') or pod.get('hostPID') or pod.get('hostIPC'):
        raise ValueError('host_namespace_requires_separate_review')
    matches = [(i, c) for i, c in enumerate(pod['containers']) if c['name'] == name]
    if len(matches) != 1:
        raise ValueError('ambiguous_container')
    i, container = matches[0]
    if not IMAGE.fullmatch(container.get('image', '')):
        raise ValueError('immutable_image_required')
    return meta, pod, i, container


def validate_observation(evidence, meta, *, now=None):
    if not isinstance(evidence, dict):
        raise ValueError('reviewed_evidence_required')
    if evidence.get('deployment_uid') != meta['uid'] or evidence.get('resource_version') != meta['resourceVersion']:
        raise ValueError('stale_deployment_evidence')
    observed = datetime.fromisoformat(evidence['observed_at'].replace('Z', '+00:00'))
    if observed.tzinfo is None:
        raise ValueError('timezone_required')
    age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
    if not 0 <= age <= 86400:
        raise ValueError('fresh_evidence_required')


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
        raise ValueError('positive_finite_measurement_required')
    return value


def quantity(value, resource):
    # Deliberately accept a small, auditable subset of Kubernetes quantities.
    # Reject exponent, signed, decimal-memory and NaN inputs rather than guessing.
    if not isinstance(value, str):
        raise ValueError('resource_quantity_must_be_string')
    if resource == 'memory':
        match = re.fullmatch(r'([1-9][0-9]*)(Ki|Mi|Gi)', value)
        if not match:
            raise ValueError('memory_requires_positive_Ki_Mi_Gi')
        return int(match[1]) * {'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3}[match[2]]
    match = re.fullmatch(r'([1-9][0-9]*)(m?)', value)
    if not match:
        raise ValueError('cpu_requires_positive_cores_or_millicores')
    return int(match[1]) * (1 if match[2] else 1000)


def validate_resources(resources, baseline):
    if not isinstance(resources, dict) or set(resources) - {'requests', 'limits'}:
        raise ValueError('unsupported_resources')
    if _positive(baseline['duration_seconds']) < 86400 or _positive(baseline['samples']) < 288:
        raise ValueError('representative_24_hour_baseline_required')
    p95 = _positive(baseline['memory_p95_bytes'])
    peak = _positive(baseline['memory_peak_bytes'])
    cpu = _positive(baseline['cpu_p95_millicores'])
    if p95 > peak:
        raise ValueError('invalid_memory_baseline')
    for section, fields in resources.items():
        if not isinstance(fields, dict) or set(fields) - {'memory', 'cpu'}:
            raise ValueError('unsupported_resource_field')
        for field, value in fields.items():
            quantity(value, field)
    request = resources['requests']
    limit = resources['limits']
    if quantity(request['memory'], 'memory') < p95 or quantity(request['cpu'], 'cpu') < cpu:
        raise ValueError('requests_below_measured_p95')
    if quantity(limit['memory'], 'memory') < max(peak * 1.5, quantity(request['memory'], 'memory')):
        raise ValueError('memory_limit_requires_peak_headroom')
    if 'cpu' in limit and quantity(limit['cpu'], 'cpu') < quantity(request['cpu'], 'cpu'):
        raise ValueError('cpu_limit_below_request')


def prepare(snapshot, evidence=None):
    meta, pod, i, container = validate_snapshot(snapshot)
    if snapshot['spec'].get('strategy', {}).get('type') != 'Recreate' or type(snapshot['spec'].get('replicas')) is not int or snapshot['spec']['replicas'] not in (0, 1):
        raise ValueError('single_writer_recreate_required')
    validate_observation(evidence, meta)
    if evidence.get('image') != container['image']:
        raise ValueError('image_bound_evidence_required')
    ready = evidence.get('ready', {})
    if ready.get('path') != '/ready' or type(ready.get('status')) is not int or ready['status'] != 200:
        raise ValueError('ready_probe_200_required')
    probe = container.get('readinessProbe', {}).get('httpGet')
    if not isinstance(probe, dict) or not probe.get('port') or probe.get('scheme', 'HTTP') != 'HTTP' or probe.get('host'):
        raise ValueError('existing_direct_http_readiness_probe_required')
    if evidence.get('read_only_rootfs_verified') is not True:
        raise ValueError('read_only_rootfs_image_test_required')
    mounts = container.get('volumeMounts', [])
    volumes = {v['name']: v for v in pod.get('volumes', [])}
    writable = [m for m in mounts if not m.get('readOnly') and m['name'] in volumes]
    if not any(m['mountPath'] == '/tmp' and 'emptyDir' in volumes[m['name']] for m in writable):
        raise ValueError('writable_tmp_emptydir_required')
    if not any('persistentVolumeClaim' in volumes[m['name']] for m in writable):
        raise ValueError('writable_data_pvc_required')
    requested = evidence['resources']
    validate_resources(requested, evidence['baseline'])
    existing = container.get('resources', {})
    if not isinstance(existing, dict):
        raise ValueError('invalid_existing_resources')
    resources = deepcopy(existing)
    for section, fields in requested.items():
        target = resources.setdefault(section, {})
        for field, value in fields.items():
            target.setdefault(field, value)
    validate_resources(resources, evidence['baseline'])
    base = f'/spec/template/spec/containers/{i}'
    patch = [
        {'op': 'test', 'path': '/metadata/uid', 'value': meta['uid']},
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': meta['resourceVersion']},
        {'op': 'test', 'path': base + '/name', 'value': meta['name']},
        # UID/resourceVersion already guard the complete Deployment. Never
        # echo the whole pod spec: env values may contain runtime credentials.
        {'op': 'test', 'path': base + '/image', 'value': container['image']},
    ]
    if 'resources' not in container:
        patch.append({'op': 'add', 'path': base + '/resources', 'value': resources})
    else:
        for section, fields in resources.items():
            if section not in existing:
                patch.append({'op': 'add', 'path': base + '/resources/' + section, 'value': fields})
            else:
                for field, value in fields.items():
                    if field not in existing[section]:
                        patch.append({'op': 'add', 'path': base + '/resources/' + section + '/' + field, 'value': value})
    security = container.get('securityContext', {})
    if not isinstance(security, dict):
        raise ValueError('invalid_security_context')
    pod_security = pod.get('securityContext', {})
    if pod_security.get('runAsNonRoot') is not True or pod_security.get('seccompProfile') != {'type': 'RuntimeDefault'} or security.get('privileged', False) is not False:
        raise ValueError('nonroot_seccomp_nonprivileged_profile_required')
    # Container fields override the pod profile; preserving a root or
    # Unconfined override would falsely label an unsafe container as hardened.
    effective_nonroot = security.get('runAsNonRoot', pod_security.get('runAsNonRoot'))
    effective_user = security.get('runAsUser', pod_security.get('runAsUser'))
    effective_seccomp = security.get('seccompProfile', pod_security.get('seccompProfile'))
    if effective_nonroot is not True or effective_user == 0 or effective_seccomp != {'type': 'RuntimeDefault'} or security.get('procMount', 'Default') != 'Default':
        raise ValueError('effective_container_security_profile_required')
    secure = {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}
    for field, value in secure.items():
        if field in security and security[field] != value:
            raise ValueError('conflicting_security_requires_separate_review')
    if 'securityContext' not in container:
        patch.append({'op': 'add', 'path': base + '/securityContext', 'value': secure})
    else:
        for field, value in secure.items():
            if field not in security:
                patch.append({'op': 'add', 'path': base + '/securityContext/' + field, 'value': value})
    if pod.get('automountServiceAccountToken') is not False:
        patch.append({'op': 'add', 'path': '/spec/template/spec/automountServiceAccountToken', 'value': False})
    if probe.get('path') != '/ready':
        patch.append({'op': 'add', 'path': base + '/readinessProbe/httpGet/path', 'value': '/ready'})
    return {'deployment': meta['name'], 'namespace': 'personal-server', 'patch_type': 'json', 'patch': patch,
            'approval_required': True, 'preconditions': ['revalidate snapshot and evidence immediately before approved apply', 'node allocatable and aggregate requests/limits review', 'single-writer sequential rollout with rollback and external health checks'],
            'baseline': deepcopy(evidence['baseline']), 'resources': resources}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot')
    parser.add_argument('--evidence')
    parser.add_argument('--describe-input', action='store_true')
    args = parser.parse_args()
    if args.describe_input:
        print(json.dumps({'deployment_uid': 'REQUIRED', 'resource_version': 'REQUIRED', 'image': 'REQUIRED_IMMUTABLE_DIGEST', 'observed_at': 'REQUIRED_UTC_ISO8601', 'ready': {'path': '/ready', 'status': None}, 'read_only_rootfs_verified': False, 'baseline': {'duration_seconds': None, 'samples': None, 'memory_p95_bytes': None, 'memory_peak_bytes': None, 'cpu_p95_millicores': None}, 'resources': {'requests': {'cpu': 'REVIEW_FROM_P95', 'memory': 'REVIEW_FROM_P95'}, 'limits': {'memory': 'REVIEW_FROM_PEAK_WITH_HEADROOM'}}}, indent=2))
        return
    if not args.snapshot or not args.evidence:
        parser.error('--snapshot and --evidence are required')
    try:
        with open(args.snapshot) as source:
            snapshot = json.load(source)
        with open(args.evidence) as source:
            evidence = json.load(source)
        result = prepare(snapshot, evidence)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        parser.exit(1, 'hardening_prepare=FAIL; verify snapshot and evidence input contract\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
