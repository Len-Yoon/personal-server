import importlib.util
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'infra/k8s/tools/prepare-portal-hardening.py'


def load():
    spec = importlib.util.spec_from_file_location('portal_hardening', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply_patch(document, patch):
    result = deepcopy(document)
    for operation in patch:
        keys = operation['path'].strip('/').split('/')
        parent = result
        for key in keys[:-1]:
            parent = parent[int(key)] if isinstance(parent, list) else parent[key]
        key = int(keys[-1]) if isinstance(parent, list) else keys[-1]
        if operation['op'] == 'test':
            if parent[key] != operation['value']:
                raise ValueError('CAS failed')
        elif operation['op'] == 'add':
            if isinstance(parent, list):
                parent.insert(key, deepcopy(operation['value']))
            else:
                parent[key] = deepcopy(operation['value'])
        elif operation['op'] == 'remove':
            if isinstance(parent, list):
                parent.pop(key)
            else:
                del parent[key]
        else:
            raise ValueError('unexpected patch operation')
    return result


class PortalHardeningTests(unittest.TestCase):
    def fixture(self):
        image = 'example.invalid/portal@sha256:' + 'a' * 64
        snapshot = {'kind': 'Deployment', 'metadata': {'name': 'portal-web', 'namespace': 'personal-server', 'uid': 'fixture', 'resourceVersion': '10'}, 'spec': {'replicas': 1, 'strategy': {'type': 'Recreate'}, 'template': {'spec': {'automountServiceAccountToken': False, 'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001}, 'containers': [{'name': 'portal-web', 'image': image, 'resources': {'requests': {'cpu': '25m', 'memory': '128Mi'}, 'limits': {'memory': '256Mi'}}, 'env': [{'name': 'FIXTURE_SECRET', 'value': 'do-not-echo-private-fixture'}], 'readinessProbe': {'httpGet': {'path': '/ready', 'port': 8000}}, 'volumeMounts': [{'name': 'files', 'mountPath': '/data/files'}, {'name': 'state', 'mountPath': '/var/lib/portal'}]}], 'volumes': [{'name': 'files', 'persistentVolumeClaim': {'claimName': 'fixture-files'}}, {'name': 'state', 'persistentVolumeClaim': {'claimName': 'fixture-state'}}]}}}}
        evidence = {'deployment_uid': 'fixture', 'resource_version': '10', 'image': image, 'observed_at': datetime.now(timezone.utc).isoformat(), 'isolated': {'image': image, 'platform': 'linux/amd64', 'read_only_rootfs': True, 'root_write_denied': True, 'uid': 10001, 'health_status': 200, 'ready_status': 200, 'no_production_data': True, 'write_paths_complete': True, 'configured_write_paths_within_mounts': True, 'write_path_sources_reviewed': True, 'writable_paths': {path: {'create_write_delete': True} for path in ['/data/files', '/var/lib/portal', '/tmp']}}}
        return snapshot, evidence

    def test_preparation_is_offline_and_preserves_image_data_env_resources_and_writer(self):
        module = load(); snapshot, evidence = self.fixture(); original = deepcopy(snapshot)
        result = module.prepare(snapshot, evidence)
        hardened = apply_patch(snapshot, result['patch'])
        self.assertEqual(snapshot, original)
        pod = hardened['spec']['template']['spec']; container = pod['containers'][0]
        self.assertEqual(pod['securityContext']['seccompProfile'], {'type': 'RuntimeDefault'})
        self.assertEqual(container['securityContext'], {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}})
        self.assertEqual(pod['volumes'][-1], {'name': 'portal-hardening-tmp', 'emptyDir': {}})
        self.assertEqual(container['volumeMounts'][-1], {'name': 'portal-hardening-tmp', 'mountPath': '/tmp'})
        self.assertEqual(container['image'], original['spec']['template']['spec']['containers'][0]['image'])
        self.assertEqual(container['env'], original['spec']['template']['spec']['containers'][0]['env'])
        self.assertEqual(container['resources'], original['spec']['template']['spec']['containers'][0]['resources'])
        self.assertEqual(hardened['spec']['replicas'], 1)
        self.assertEqual(hardened['spec']['strategy'], {'type': 'Recreate'})
        self.assertNotIn('do-not-echo-private-fixture', json.dumps(result))
        self.assertTrue(result['approval_required'])

    def test_rollback_removes_exact_added_fields_and_restores_original_spec(self):
        module = load(); snapshot, evidence = self.fixture()
        prepared = module.prepare(snapshot, evidence); hardened = apply_patch(snapshot, prepared['patch'])
        hardened['metadata']['resourceVersion'] = '11'
        inverse = module.prepare_rollback(hardened, prepared['rollback_record'])
        restored = apply_patch(hardened, inverse['patch'])
        self.assertEqual(restored['spec'], snapshot['spec'])
        self.assertTrue(all(op['op'] in {'test', 'remove'} for op in inverse['patch']))
        self.assertFalse(any(op['path'] in {'/spec', '/spec/template/spec'} for op in inverse['patch']))

    def test_existing_compliant_security_is_preserved_during_rollback(self):
        module = load(); snapshot, evidence = self.fixture(); pod = snapshot['spec']['template']['spec']
        pod['securityContext']['seccompProfile'] = {'type': 'RuntimeDefault'}
        pod['containers'][0]['securityContext'] = {'allowPrivilegeEscalation': False, 'runAsUser': 10001}
        result = module.prepare(snapshot, evidence); hardened = apply_patch(snapshot, result['patch'])
        restored = apply_patch(hardened, module.prepare_rollback(hardened, result['rollback_record'])['patch'])
        self.assertEqual(restored['spec'], snapshot['spec'])

    def test_readonly_proof_must_cover_every_existing_writable_mount_and_tmp(self):
        module = load(); snapshot, evidence = self.fixture()
        mutations = [lambda e: e['isolated'].update(read_only_rootfs=False), lambda e: e['isolated'].update(root_write_denied=False), lambda e: e['isolated'].update(no_production_data=False), lambda e: e['isolated'].update(write_paths_complete=False), lambda e: e['isolated'].update(configured_write_paths_within_mounts=False), lambda e: e['isolated'].update(write_path_sources_reviewed=False), lambda e: e['isolated']['writable_paths'].pop('/var/lib/portal'), lambda e: e['isolated']['writable_paths'].pop('/tmp'), lambda e: e['isolated']['writable_paths']['/data/files'].update(create_write_delete=False), lambda e: e['isolated'].update(ready_status=503), lambda e: e['isolated'].update(uid=0)]
        for mutate in mutations:
            invalid = deepcopy(evidence); mutate(invalid)
            with self.assertRaises(ValueError): module.prepare(snapshot, invalid)

    def test_evidence_identity_freshness_and_immutable_image_are_required(self):
        module = load(); snapshot, evidence = self.fixture()
        for key, value in [('deployment_uid', 'other'), ('resource_version', '9'), ('image', 'other'), ('observed_at', (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()), ('observed_at', '2026-10-10T00:00:00')]:
            invalid = deepcopy(evidence); invalid[key] = value
            with self.assertRaises(ValueError): module.prepare(snapshot, invalid)
        evidence['isolated']['image'] = 'other'
        with self.assertRaises(ValueError): module.prepare(snapshot, evidence)

    def test_tmp_name_path_and_nested_mount_collisions_are_rejected(self):
        module = load(); snapshot, evidence = self.fixture()
        for path in ['/tmp', '/tmp/subdir', '/']:
            invalid = deepcopy(snapshot); invalid['spec']['template']['spec']['containers'][0]['volumeMounts'].append({'name': 'files', 'mountPath': path})
            with self.assertRaises(ValueError): module.prepare(invalid, evidence)
        snapshot['spec']['template']['spec']['volumes'].append({'name': 'portal-hardening-tmp', 'emptyDir': {}})
        with self.assertRaises(ValueError): module.prepare(snapshot, evidence)

    def test_conflicting_or_effectively_privileged_security_is_rejected(self):
        module = load(); snapshot, evidence = self.fixture()
        for security in [{'allowPrivilegeEscalation': True}, {'readOnlyRootFilesystem': False}, {'privileged': True}, {'runAsNonRoot': False}, {'runAsUser': 0}, {'seccompProfile': {'type': 'Unconfined'}}, {'capabilities': {'add': ['SYS_ADMIN']}}, {'procMount': 'Unmasked'}]:
            invalid = deepcopy(snapshot); invalid['spec']['template']['spec']['containers'][0]['securityContext'] = security
            with self.assertRaises(ValueError): module.prepare(invalid, evidence)
        snapshot['spec']['template']['spec']['securityContext']['seccompProfile'] = {'type': 'Unconfined'}
        with self.assertRaises(ValueError): module.prepare(snapshot, evidence)

    def test_token_host_sidecar_and_writer_expansion_are_rejected(self):
        module = load(); snapshot, evidence = self.fixture()
        mutations = [lambda s: s['spec'].update(replicas=2), lambda s: s['spec'].update(strategy={'type': 'RollingUpdate'}), lambda s: s['spec']['template']['spec'].update(automountServiceAccountToken=True), lambda s: s['spec']['template']['spec'].update(hostNetwork=True), lambda s: s['spec']['template']['spec'].update(hostPID=True), lambda s: s['spec']['template']['spec']['containers'][0].update(ports=[{'containerPort':8000,'hostPort':8000}]), lambda s: s['spec']['template']['spec'].update(initContainers=[{'name': 'extra'}]), lambda s: s['spec']['template']['spec']['volumes'].append({'name': 'host', 'hostPath': {'path': '/'}}), lambda s: s['spec']['template']['spec']['volumes'].append({'name': 'token', 'projected': {'sources': [{'serviceAccountToken': {'path': 'token'}}]}})]
        for mutate in mutations:
            invalid = deepcopy(snapshot); mutate(invalid)
            with self.assertRaises(ValueError): module.prepare(invalid, evidence)

    def test_rollback_refuses_concurrent_protected_or_managed_changes(self):
        module = load(); snapshot, evidence = self.fixture(); result = module.prepare(snapshot, evidence)
        hardened = apply_patch(snapshot, result['patch'])
        mutations = [lambda s: s['metadata'].update(uid='other'), lambda s: s['spec']['template']['spec']['containers'][0].update(image='foreign:latest'), lambda s: s['spec']['template']['spec']['containers'][0]['resources']['limits'].update(memory='512Mi'), lambda s: s['spec']['template']['spec']['containers'][0]['securityContext'].update(readOnlyRootFilesystem=False), lambda s: s['spec']['template']['spec']['volumes'].append({'name': 'other', 'emptyDir': {}})]
        for mutate in mutations:
            invalid = deepcopy(hardened); mutate(invalid)
            with self.assertRaises(ValueError): module.prepare_rollback(invalid, result['rollback_record'])

    def test_rollback_record_cannot_delete_unrelated_fields(self):
        module = load(); snapshot, evidence = self.fixture(); result = module.prepare(snapshot, evidence)
        hardened = apply_patch(snapshot, result['patch']); record = deepcopy(result['rollback_record'])
        record['added_fields'].append({'path': '/spec/template/spec/containers/0/env', 'value': snapshot['spec']['template']['spec']['containers'][0]['env']})
        with self.assertRaises(ValueError): module.prepare_rollback(hardened, record)

    def test_snapshot_resource_version_race_fails_patch_test(self):
        module = load(); snapshot, evidence = self.fixture(); result = module.prepare(snapshot, evidence)
        snapshot['metadata']['resourceVersion'] = '11'
        with self.assertRaises(ValueError): apply_patch(snapshot, result['patch'])

    def test_cli_fails_without_sensitive_output_on_bad_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root/'snapshot.json').write_text('"do-not-echo-private-fixture"'); (root/'evidence.json').write_text('{}')
            result = subprocess.run(['python3', str(SCRIPT), '--snapshot', str(root/'snapshot.json'), '--evidence', str(root/'evidence.json')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('do-not-echo-private-fixture', result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_cli_prepare_and_rollback_exchange_only_review_artifacts(self):
        snapshot, evidence = self.fixture()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'snapshot.json').write_text(json.dumps(snapshot))
            (root/'evidence.json').write_text(json.dumps(evidence))
            prepared = subprocess.run(['python3', str(SCRIPT), '--snapshot', str(root/'snapshot.json'), '--evidence', str(root/'evidence.json')], capture_output=True, text=True, check=True)
            result = json.loads(prepared.stdout)
            hardened = apply_patch(snapshot, result['patch'])
            hardened['metadata']['resourceVersion'] = '12'
            (root/'snapshot.json').write_text(json.dumps(hardened))
            (root/'rollback.json').write_text(json.dumps(result['rollback_record']))
            inverse = subprocess.run(['python3', str(SCRIPT), '--snapshot', str(root/'snapshot.json'), '--rollback-record', str(root/'rollback.json')], capture_output=True, text=True, check=True)
            restored = apply_patch(hardened, json.loads(inverse.stdout)['patch'])
        self.assertEqual(restored['spec'], snapshot['spec'])
        self.assertNotIn('do-not-echo-private-fixture', prepared.stdout + inverse.stdout)


if __name__ == '__main__':
    unittest.main()
