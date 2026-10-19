#!/usr/bin/env python3
"""Inspect or CAS-add the audit's one named ConfigMap GET permission.

No manifest apply, schedule changes, Secret reads, or report writes.
--apply requires the UID and rules hash from a reviewed --check result.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys

KUBECTL = ('sudo', '-n', 'k3s', 'kubectl', '--request-timeout=10s')
NAMESPACE = 'monitoring'
ROLE = 'quarterly-sre-audit-status'
SERVICE_ACCOUNT = 'quarterly-sre-audit'
RULE = {'apiGroups': [''], 'resources': ['configmaps'],
        'resourceNames': ['slo-daily-evidence'], 'verbs': ['get']}


def rules_hash(rules):
    return hashlib.sha256(json.dumps(rules, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def command(args, *, run=subprocess.run):
    result = run([*KUBECTL, *args], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError('kubernetes_request_failed')
    return result.stdout


def read(kind, name=None, *, run=subprocess.run):
    return json.loads(command(['-n', NAMESPACE, 'get', kind, *([name] if name else []), '-o', 'json'], run=run))


def binding_valid(binding):
    return (binding.get('roleRef') == {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': ROLE}
            and binding.get('subjects') == [{'kind': 'ServiceAccount', 'name': SERVICE_ACCOUNT, 'namespace': NAMESPACE}])


def inspect(*, run=subprocess.run):
    role = read('role', ROLE, run=run)
    if (role.get('metadata', {}).get('name') != ROLE or role['metadata'].get('namespace') != NAMESPACE
            or not isinstance(role.get('rules'), list)):
        raise ValueError('unexpected_role')
    if not binding_valid(read('rolebinding', ROLE, run=run)):
        raise ValueError('unexpected_binding')
    bindings = read('rolebindings', run=run)['items']
    linked = [binding for binding in bindings if binding.get('roleRef') ==
              {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': ROLE}]
    if len(linked) != 1 or linked[0].get('metadata', {}).get('name') != ROLE or not binding_valid(linked[0]):
        raise ValueError('unexpected_role_recipients')
    return role


def plan(role):
    return {'role_uid': role['metadata']['uid'], 'rules_sha256': rules_hash(role['rules']),
            'required_rule_present': RULE in role['rules'], 'scope': 'one_named_configmap_get'}


def access_allowed(*, run=subprocess.run):
    args = [*KUBECTL, 'auth', 'can-i', 'get', 'configmap/slo-daily-evidence', '-n', NAMESPACE,
            '--as=system:serviceaccount:monitoring:quarterly-sre-audit']
    result = run(args, capture_output=True, text=True, timeout=15)
    answer = result.stdout.strip()
    if (result.returncode, answer) not in ((0, 'yes'), (1, 'no')):
        raise RuntimeError('access_check_unavailable')
    return answer == 'yes'


def apply(expected_uid, expected_rules_sha256, *, run=subprocess.run):
    role = inspect(run=run)
    if role['metadata']['uid'] != expected_uid or rules_hash(role['rules']) != expected_rules_sha256:
        raise ValueError('approved_role_drift')
    if RULE in role['rules']:
        if not access_allowed(run=run):
            raise RuntimeError('existing_access_unverified')
        return {'status': 'already_present', **plan(role)}
    jobs = read('jobs', run=run)['items']
    for job in jobs:
        sa = job.get('spec', {}).get('template', {}).get('spec', {}).get('serviceAccountName')
        complete = any(c.get('type') in ('Complete', 'Failed') and c.get('status') == 'True'
                       for c in job.get('status', {}).get('conditions', []))
        if sa in ('quarterly-sre-audit', 'quarterly-sre-audit-validation') and not complete:
            raise ValueError('audit_job_active')
    patch = [
        {'op': 'test', 'path': '/metadata/uid', 'value': expected_uid},
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': role['metadata']['resourceVersion']},
        {'op': 'test', 'path': '/rules', 'value': role['rules']},
        {'op': 'add', 'path': '/rules/-', 'value': RULE},
    ]
    command(['-n', NAMESPACE, 'patch', 'role', ROLE, '--type=json', '--patch', json.dumps(patch)], run=run)
    after = inspect(run=run)
    if after['metadata']['uid'] != expected_uid or after['rules'] != [*role['rules'], RULE]:
        raise RuntimeError('post_apply_drift')
    if not access_allowed(run=run):
        raise RuntimeError('post_apply_access_unverified')
    return {'status': 'applied', **plan(after)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-role-uid')
    parser.add_argument('--expected-rules-sha256')
    args = parser.parse_args(argv)
    if args.apply and (not re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', args.expected_role_uid or '')
                       or not re.fullmatch(r'[0-9a-f]{64}', args.expected_rules_sha256 or '')):
        parser.error('--apply requires the reviewed role UID and rules SHA256')
    try:
        result = apply(args.expected_role_uid, args.expected_rules_sha256) if args.apply else {'status': 'checked', **plan(inspect()), 'effective_get_allowed': access_allowed()}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (RuntimeError, ValueError, KeyError, TypeError, subprocess.SubprocessError, OSError):
        print('slo_audit_read_access=FAIL reason=verification_failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
