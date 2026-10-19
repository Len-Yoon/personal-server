import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('slo_audit_read_access', ROOT / 'infra/k8s/tools/slo-audit-read-access.py')
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)
UID = '743a5349-f77b-4948-9d50-a5b18a44c7f2'


class Cluster:
    def __init__(self):
        self.role = {'metadata': {'name': TOOL.ROLE, 'namespace': 'monitoring', 'uid': UID, 'resourceVersion': '100'},
                     'rules': [{'apiGroups': [''], 'resources': ['configmaps'],
                                'resourceNames': ['sre-telegram-quarterly-audit-status'], 'verbs': ['get', 'patch']}]}
        self.binding = {'metadata': {'name': TOOL.ROLE}, 'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': TOOL.ROLE},
                        'subjects': [{'kind': 'ServiceAccount', 'name': TOOL.SERVICE_ACCOUNT, 'namespace': 'monitoring'}]}
        self.other_bindings = []
        self.jobs = []
        self.writes = []
        self.calls = []
        self.concurrent_drift = False

    def run(self, args, **kwargs):
        self.calls.append(args)
        args = args[len(TOOL.KUBECTL):]
        if 'patch' in args:
            operations = json.loads(args[-1])
            if self.concurrent_drift:
                return subprocess.CompletedProcess(args, 1, '', 'private-cluster-error')
            self.writes.append(args)
            assert operations[:3] == [
                {'op': 'test', 'path': '/metadata/uid', 'value': UID},
                {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '100'},
                {'op': 'test', 'path': '/rules', 'value': self.role['rules']},
            ]
            assert operations[3] == {'op': 'add', 'path': '/rules/-', 'value': TOOL.RULE}
            self.role['rules'].append(copy.deepcopy(TOOL.RULE))
            self.role['metadata']['resourceVersion'] = '101'
            value = self.role
        elif 'auth' in args:
            value = 'yes' if TOOL.RULE in self.role['rules'] else 'no'
            return subprocess.CompletedProcess(args, 0 if value == 'yes' else 1, value + '\n', '')
        else:
            kind = args[args.index('get') + 1]
            value = {'role': self.role, 'rolebinding': self.binding, 'rolebindings': {'items': [self.binding, *self.other_bindings]}, 'jobs': {'items': self.jobs}}[kind]
        return subprocess.CompletedProcess(args, 0, json.dumps(value), '')


class AuditReadAccessTests(unittest.TestCase):
    def test_check_is_read_only_and_apply_only_adds_named_get_then_is_idempotent(self):
        cluster = Cluster()
        before = copy.deepcopy(cluster.role)
        plan = TOOL.plan(TOOL.inspect(run=cluster.run))
        self.assertFalse(plan['required_rule_present'])
        self.assertEqual(cluster.writes, [])
        result = TOOL.apply(UID, plan['rules_sha256'], run=cluster.run)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(cluster.role['rules'], [*before['rules'], TOOL.RULE])
        self.assertEqual(len(cluster.writes), 1)
        self.assertEqual(TOOL.apply(UID, result['rules_sha256'], run=cluster.run)['status'], 'already_present')
        self.assertEqual(len(cluster.writes), 1)
        self.assertTrue(any('auth' in command for command in cluster.calls))

    def test_uid_rule_binding_and_active_job_drift_block_before_mutation(self):
        for case in ('uid', 'rules', 'binding', 'extra_subject', 'extra_binding', 'active'):
            with self.subTest(case=case):
                cluster = Cluster()
                approved_hash = TOOL.rules_hash(cluster.role['rules'])
                if case == 'uid':
                    cluster.role['metadata']['uid'] = 'foreign'
                elif case == 'rules':
                    cluster.role['rules'].append({'verbs': ['list']})
                elif case == 'binding':
                    cluster.binding['roleRef']['name'] = 'foreign'
                elif case == 'extra_subject':
                    cluster.binding['subjects'].append({'kind': 'ServiceAccount', 'name': 'foreign', 'namespace': 'monitoring'})
                elif case == 'extra_binding':
                    extra = copy.deepcopy(cluster.binding)
                    extra['metadata']['name'] = 'foreign-binding'
                    extra['subjects'][0]['name'] = 'foreign'
                    cluster.other_bindings.append(extra)
                else:
                    cluster.jobs = [{'spec': {'template': {'spec': {'serviceAccountName': TOOL.SERVICE_ACCOUNT}}}}]
                with self.assertRaises(ValueError):
                    TOOL.apply(UID, approved_hash, run=cluster.run)
                self.assertEqual(cluster.writes, [])

    def test_cas_failure_is_not_retried_or_overwritten(self):
        cluster = Cluster()
        before = copy.deepcopy(cluster.role)
        cluster.concurrent_drift = True
        with self.assertRaisesRegex(RuntimeError, 'kubernetes_request_failed'):
            TOOL.apply(UID, TOOL.rules_hash(cluster.role['rules']), run=cluster.run)
        self.assertEqual(cluster.role, before)
        self.assertEqual(sum('patch' in args for args in cluster.calls), 1)

    def test_cli_errors_do_not_expose_raw_cluster_response(self):
        with patch.object(TOOL, 'inspect', side_effect=RuntimeError('private-cluster-error')):
            with patch('sys.stderr') as stderr:
                self.assertEqual(TOOL.main(['--check']), 1)
                self.assertNotIn('private-cluster-error', str(stderr.write.call_args_list))


if __name__ == '__main__':
    unittest.main()
