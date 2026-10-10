import importlib.util
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'infra/k8s/tools/prepare-portal-service-routing.py'

def load():
    spec = importlib.util.spec_from_file_location('portal_service_routing', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def apply(document, operations):
    result = deepcopy(document)
    for operation in operations:
        keys = operation['path'].strip('/').split('/'); parent = result
        for key in keys[:-1]:
            parent = parent[int(key)] if isinstance(parent, list) else parent[key]
        key = int(keys[-1]) if isinstance(parent, list) else keys[-1]
        if operation['op'] == 'test':
            if parent[key] != operation['value']:
                raise ValueError('CAS failed')
        elif operation['op'] == 'replace':
            parent[key] = deepcopy(operation['value'])
        else:
            raise ValueError('only guarded replacement allowed')
    return result

class PortalServiceRoutingTests(unittest.TestCase):
    def fixture(self):
        module = load(); image = 'example.invalid/portal@sha256:' + 'a' * 64
        snapshot = {'kind': 'Deployment', 'metadata': {'name': 'portal-web', 'namespace': 'personal-server', 'uid': 'fixture', 'resourceVersion': '10'}, 'spec': {'replicas': 1, 'strategy': {'type': 'Recreate'}, 'template': {'spec': {'containers': [{'name': 'portal-web', 'image': image, 'resources': {'limits': {'memory': '256Mi'}}, 'env': [{'name': 'PRIVATE_FIXTURE', 'value': 'never-echo-private'}] + [{'name': name, 'value': old} for name, (old, new) in module.ROUTES.items()], 'volumeMounts': [{'name': 'data', 'mountPath': '/data/files'}]}], 'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': 'fixture'}}]}}}}
        evidence = {'deployment_uid': 'fixture', 'resource_version': '10', 'image': image, 'spec_sha256': module.digest(snapshot['spec']), 'observed_at': datetime.now(timezone.utc).isoformat(), 'single_writer_verified': True, 'no_secret_access': True, 'probe_origin': 'portal-web-pod', 'routes': {name: {'old_url': old, 'direct_url': new, 'old_alias_failed': True, 'direct_status': 200} for name, (old, new) in module.ROUTES.items()}}
        return module, snapshot, evidence

    def test_prepare_changes_exact_six_literal_values_and_preserves_everything_else(self):
        m, original, evidence = self.fixture(); before = deepcopy(original)
        result = m.prepare(original, evidence); routed = apply(original, result['patch'])
        self.assertEqual(original, before)
        self.assertEqual(len([x for x in result['patch'] if x['op'] == 'replace']), 6)
        restored = deepcopy(routed)
        for index, entry in enumerate(before['spec']['template']['spec']['containers'][0]['env']):
            restored['spec']['template']['spec']['containers'][0]['env'][index] = deepcopy(entry)
        self.assertEqual(restored, before)
        self.assertNotIn('never-echo-private', json.dumps(result)); self.assertTrue(result['approval_required'])
        for item in routed['spec']['template']['spec']['containers'][0]['env'][1:]:
            self.assertEqual(item['value'], m.ROUTES[item['name']][1])

    def test_rollback_reconstructs_original_spec_with_fresh_cas(self):
        m, original, evidence = self.fixture(); result = m.prepare(original, evidence); routed = apply(original, result['patch']); routed['metadata']['resourceVersion'] = '20'
        rollback = m.prepare_rollback(routed, result['rollback_record'])
        restored = apply(routed, rollback['patch']); self.assertEqual(restored['spec'], original['spec'])
        self.assertEqual(rollback['patch'][1]['value'], '20')

    def test_wrong_identity_or_spec_or_stale_evidence_fails_closed(self):
        m, snapshot, evidence = self.fixture()
        variants = [('deployment_uid', 'other'), ('resource_version', '11'), ('image', 'different'), ('spec_sha256', '0' * 64), ('observed_at', (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()), ('observed_at', datetime.now().isoformat())]
        for key, value in variants:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                proof = deepcopy(evidence); proof[key] = value; m.prepare(snapshot, proof)

    def test_all_six_current_origin_and_failure_proofs_are_required(self):
        m, snapshot, evidence = self.fixture(); first = next(iter(m.ROUTES))
        for key, value in [('single_writer_verified', False), ('no_secret_access', False), ('probe_origin', 'outside-pod')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                proof = deepcopy(evidence); proof[key] = value; m.prepare(snapshot, proof)
        for key, value in [('old_alias_failed', False), ('direct_status', 503), ('direct_status', True), ('old_url', 'http://wrong.invalid/health'), ('direct_url', 'http://wrong.invalid/health')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                proof = deepcopy(evidence); proof['routes'][first][key] = value; m.prepare(snapshot, proof)
        del evidence['routes'][first]
        with self.assertRaises(ValueError): m.prepare(snapshot, evidence)

    def test_rejects_valuefrom_duplicate_missing_already_direct_and_other_hosts(self):
        m, snapshot, evidence = self.fixture()
        for mutation in ('valueFrom', 'duplicate', 'missing', 'direct', 'host'):
            current = deepcopy(snapshot); env = current['spec']['template']['spec']['containers'][0]['env']; target = env[1]
            if mutation == 'valueFrom': target['valueFrom'] = {'secretKeyRef': {'name': 'fixture', 'key': 'url'}}
            elif mutation == 'duplicate': env.append(deepcopy(target))
            elif mutation == 'missing': env.pop(1)
            elif mutation == 'direct': target['value'] = m.ROUTES[target['name']][1]
            else: target['value'] = 'http://unknown.invalid/api/search'
            proof = deepcopy(evidence); proof['spec_sha256'] = m.digest(current['spec'])
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): m.prepare(current, proof)

    def test_requires_single_portal_writer_and_immutable_image(self):
        m, snapshot, evidence = self.fixture()
        for mutation in ('replicas', 'strategy', 'container', 'image'):
            current = deepcopy(snapshot)
            if mutation == 'replicas': current['spec']['replicas'] = 2
            elif mutation == 'strategy': current['spec']['strategy']['type'] = 'RollingUpdate'
            elif mutation == 'container': current['spec']['template']['spec']['containers'].append({'name': 'extra'})
            else: current['spec']['template']['spec']['containers'][0]['image'] = 'mutable:latest'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): m.prepare(current, evidence)

    def test_patch_cas_rejects_resource_version_image_and_old_url_races(self):
        m, snapshot, evidence = self.fixture(); patch = m.prepare(snapshot, evidence)['patch']
        for mutation in ('rv', 'image', 'url'):
            current = deepcopy(snapshot)
            if mutation == 'rv': current['metadata']['resourceVersion'] = '11'
            elif mutation == 'image': current['spec']['template']['spec']['containers'][0]['image'] += 'different'
            else: current['spec']['template']['spec']['containers'][0]['env'][1]['value'] = 'changed'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): apply(current, patch)

    def test_rollback_rejects_changed_managed_or_protected_spec(self):
        m, snapshot, evidence = self.fixture(); result = m.prepare(snapshot, evidence); routed = apply(snapshot, result['patch'])
        for mutation in ('url', 'resources', 'pvc', 'envorder', 'image'):
            current = deepcopy(routed); container = current['spec']['template']['spec']['containers'][0]
            if mutation == 'url': container['env'][1]['value'] = 'changed'
            elif mutation == 'resources': container['resources'] = {}
            elif mutation == 'pvc': current['spec']['template']['spec']['volumes'][0]['persistentVolumeClaim']['claimName'] = 'changed'
            elif mutation == 'envorder': container['env'].reverse()
            else: container['image'] = 'example.invalid/portal@sha256:' + 'b' * 64
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): m.prepare_rollback(current, result['rollback_record'])

    def test_untrusted_rollback_cannot_change_arbitrary_env_or_target(self):
        m, snapshot, evidence = self.fixture(); result = m.prepare(snapshot, evidence); routed = apply(snapshot, result['patch'])
        for mutation in ('path', 'name', 'old', 'new', 'duplicate', 'incomplete'):
            record = deepcopy(result['rollback_record']); item = record['changed_fields'][0]
            if mutation == 'path': item['path'] = '/spec/template/spec/containers/0/env/0/value'
            elif mutation == 'name': item['name'] = 'PRIVATE_FIXTURE'
            elif mutation == 'old': item['old_value'] = 'arbitrary'
            elif mutation == 'new': item['new_value'] = 'arbitrary'
            elif mutation == 'duplicate': record['changed_fields'].append(deepcopy(item))
            else: record['changed_fields'].pop()
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): m.prepare_rollback(routed, record)

    def test_cli_failure_does_not_echo_secret_input(self):
        _, snapshot, evidence = self.fixture(); evidence['spec_sha256'] = 'bad'
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'snapshot.json'; proof = Path(directory) / 'evidence.json'; source.write_text(json.dumps(snapshot)); proof.write_text(json.dumps(evidence))
            result = subprocess.run(['python3', str(SCRIPT), '--snapshot', str(source), '--evidence', str(proof)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1); self.assertNotIn('never-echo-private', result.stdout + result.stderr)

if __name__ == '__main__': unittest.main()
