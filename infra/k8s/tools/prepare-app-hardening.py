#!/usr/bin/env python3
"""Generate guarded JSON patches from read-only Deployment snapshots. Never apply."""
import argparse
import json

APPS = {'portal-web': ('128Mi', '384Mi'), 'book-memo': ('64Mi', '256Mi'), 'youtube-memo': ('64Mi', '256Mi'), 'crawler-worker': ('128Mi', '384Mi')}


def prepare(snapshot):
    name = snapshot['metadata']['name']
    if snapshot.get('kind') != 'Deployment' or name not in APPS or snapshot['metadata'].get('namespace') != 'personal-server':
        raise ValueError('unsupported_deployment')
    meta = snapshot['metadata']
    if not meta.get('uid') or not meta.get('resourceVersion'):
        raise ValueError('live_snapshot_required')
    pod = snapshot['spec']['template']['spec']
    containers = pod['containers']
    matches = [i for i, c in enumerate(containers) if c['name'] == name]
    if len(matches) != 1:
        raise ValueError('ambiguous_container')
    i = matches[0]
    c = containers[i]
    base = f'/spec/template/spec/containers/{i}'
    patch = [{'op': 'test', 'path': '/metadata/uid', 'value': meta['uid']}, {'op': 'test', 'path': '/metadata/resourceVersion', 'value': meta['resourceVersion']}, {'op': 'test', 'path': base+'/name', 'value': name}]
    requests, limits = APPS[name]
    resources = c.get('resources') or {}
    if 'resources' not in c:
        patch.append({'op': 'add', 'path': base+'/resources', 'value': {}})
    for key, defaults in [('requests', {'cpu': '50m', 'memory': requests}), ('limits', {'cpu': '500m', 'memory': limits})]:
        if key not in resources:
            patch.append({'op': 'add', 'path': base+'/resources/'+key, 'value': defaults})
        else:
            for field, value in defaults.items():
                if field not in resources[key]:
                    patch.append({'op': 'add', 'path': base+'/resources/'+key+'/'+field, 'value': value})
    # Existing security profiles are preserved. Portal's missing container
    # profile receives the same non-privileged defaults as other app manifests.
    if 'securityContext' not in c:
        patch.append({'op': 'add', 'path': base+'/securityContext', 'value': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}})
    if 'readinessProbe' in c and 'httpGet' in c['readinessProbe']:
        patch.append({'op': 'replace', 'path': base+'/readinessProbe/httpGet/path', 'value': '/ready'})
    return {'deployment': name, 'namespace': 'personal-server', 'patch_type': 'json', 'patch': patch, 'approval_required': True, 'preconditions': ['immutable image with /ready', 'writable data and tmp mounts', 'load and memory baseline', 'sequential rollout and external health checks']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True)
    args = parser.parse_args()
    try:
        with open(args.snapshot) as source:
            result = prepare(json.load(source))
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(1, 'hardening_prepare=FAIL\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
