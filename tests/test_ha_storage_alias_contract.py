"""Synthetic models only: no collection, live handles, subprocesses or writes."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('storage_alias', ROOT / 'infra/k8s/tools/n100_ha_storage_alias_contract.py')
alias = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alias)


def fixture():
    request = dict(schema_version=1, kind='ha-storage-alias-model-request', nonce='a' * 64,
                   source_sha256='b' * 64, session='c' * 64, observed_from=10.0,
                   observed_until=10.1, deadline=11.0, providers={p: 'complete' for p in alias.PROVIDERS},
                   handles=[], footprints=[], actors=[], edges=[], states=[])
    for index, (service, categories) in enumerate(sorted(alias.STATE_CATALOG.items())):
        h, f, a = 'h' + str(index), 'f' + str(index), 'a' + str(index)
        identity = dict(kind='directory', links=2, redirected=False, filesystem='ext4',
                        boot='d' * 64, namespace='e' * 64, device='8:1', root='/state/' + service, inode=index + 1)
        request['handles'].append(dict(id=h, session=request['session'], provider='linux',
                                       opened_at=9.0, held_until=10.2, before=identity, after=deepcopy(identity)))
        request['footprints'].append(dict(id=f, root=h, members=[h], directories=[dict(directory=h, children=[])], unresolved=[]))
        request['actors'].append(dict(id=a, service=service, state='active'))
        request['edges'].append(dict(source=a, target=f, kind='write'))
        for category in sorted(categories):
            request['states'].append(dict(service=service, category=category, footprint=f, owner=a,
                                          coverage='create_parent' if category.endswith('_parent') else 'subtree'))
    return request


class AliasContractTest(unittest.TestCase):
    def run_model(self, request=None, **overrides):
        values = dict(expected_nonce='a' * 64, expected_source_sha256='b' * 64, now=10.2,
                      consumed_nonces=set(), clock=lambda: 0.0)
        values.update(overrides)
        return alias.evaluate(fixture() if request is None else request, **values)

    def blocked(self, request, reason=None, **overrides):
        result = self.run_model(request, **overrides)
        self.assertEqual(result['status'], 'blocked', result)
        if reason:
            self.assertEqual(result['reason'], reason)
        self.assertFalse(result['authority']); self.assertFalse(result['ready']); self.assertFalse(result['supports'])
        return result

    def test_closed_seven_service_model_is_non_authority_and_minimum_catalog_only(self):
        report = self.run_model()
        self.assertEqual(report['status'], 'modeled_safe', report)
        self.assertGreater(report['relations']['disjoint'], 0)
        self.assertGreater(report['relations']['overlap'], 0)
        self.assertEqual(report['state_catalog'], 'declared_minimum_only')
        self.assertFalse(report['authority']); self.assertFalse(report['ready']); self.assertFalse(report['supports'])
        self.assertNotIn('verified', report); self.assertNotIn('complete', report); self.assertNotIn('nonce', report)

    def test_existing_authority_guard_rejects_diagnostic(self):
        guard_spec = importlib.util.spec_from_file_location('old_guard', ROOT / 'infra/k8s/ha_control/legacy_guard.py')
        guard = importlib.util.module_from_spec(guard_spec); guard_spec.loader.exec_module(guard)
        with self.assertRaises(guard.GuardBlocked):
            guard.verify(self.run_model())

    def test_nonce_consumed_once_including_failed_attempt(self):
        consumed = set(); request = fixture(); request['providers']['docker'] = 'incomplete'
        self.blocked(request, 'incomplete_provider', consumed_nonces=consumed)
        self.blocked(fixture(), 'nonce_reused', consumed_nonces=consumed)
        self.assertEqual(consumed, {'a' * 64})

    def test_nonce_source_and_session_substitution_fail(self):
        for key, reason in [('nonce', 'nonce_mismatch'), ('source_sha256', 'source_mismatch')]:
            request = fixture(); request[key] = 'f' * 64
            self.blocked(request, reason)
        request = fixture(); request['handles'][0]['session'] = 'f' * 64
        self.blocked(request)

    def test_all_providers_and_all_seven_state_categories_required(self):
        for provider in alias.PROVIDERS:
            request = fixture(); request['providers'][provider] = 'incomplete'
            self.blocked(request, 'incomplete_provider')
        for service in alias.STATE_CATALOG:
            request = fixture(); request['states'] = [s for s in request['states'] if s['service'] != service]
            self.blocked(request, 'seven_service_state_coverage')
        request = fixture(); request['states'].pop()
        self.blocked(request, 'seven_service_state_coverage')

    def test_identity_replacement_and_handle_lifetime_fail(self):
        request = fixture(); request['handles'][0]['after']['inode'] += 1
        self.blocked(request, 'handle_identity_changed')
        for field, value in [('opened_at', 10.01), ('held_until', 10.05)]:
            request = fixture(); request['handles'][0][field] = value
            self.blocked(request, 'handle_lifetime_incomplete')

    def test_windows_equal_open_handle_identity_overlaps_but_unequal_is_unknown(self):
        h = fixture()['handles'][0]
        h['provider'] = 'windows'
        h['before'] = dict(kind='directory', links=1, redirected=False, backend='local_ntfs', volume='f' * 64, file_id='1' * 32)
        h['after'] = deepcopy(h['before'])
        other = deepcopy(h); other['id'] = 'other'
        self.assertEqual(alias.file_relation(h, other), 'overlap')
        other['before']['file_id'] = '2' * 32
        self.assertEqual(alias.file_relation(h, other), 'unknown')
        other['before'] = deepcopy(h['before']); other['before']['backend'] = 'remote'
        self.assertEqual(alias.file_relation(h, other), 'unknown')

    def test_no_windows_root_id_or_cross_namespace_attestation_bypass(self):
        for mode in ['windows', '9p', 'namespace', 'device']:
            request = fixture(); h = request['handles'][0]
            if mode == 'windows':
                h['provider'] = 'windows'
                h['before'] = dict(kind='directory', links=1, redirected=False, backend='local_ntfs', volume='f' * 64, file_id='1' * 32)
            elif mode == '9p':
                h['before']['filesystem'] = '9p'
            elif mode == 'namespace':
                h['before']['namespace'] = 'f' * 64
            else:
                h['before']['device'] = '9:2'
            h['after'] = deepcopy(h['before'])
            self.blocked(request, 'storage_relation_unknown')
        request = fixture(); request['footprints'][0]['attested_disjoint'] = True
        self.blocked(request, 'invalid_schema')

    def test_directory_parent_ancestry_and_overlapping_bind_alias_block_other_owner(self):
        for root, inode in [('/state', 99), (fixture()['handles'][1]['before']['root'], 99), ('/unrelated', 2)]:
            request = fixture(); request['handles'][0]['before'].update(root=root, inode=inode)
            request['handles'][0]['after'] = deepcopy(request['handles'][0]['before'])
            self.blocked(request, 'competing_writer_or_recreator')

    def test_unclosed_directory_hardlink_reparse_and_submount_are_unknown(self):
        for unresolved in ['submount', 'hardlink', 'reparse', 'enumeration', 'missing_parent', 'cross_binding']:
            request = fixture(); request['footprints'][0]['unresolved'] = [unresolved]
            self.blocked(request, 'storage_relation_unknown')
        request = fixture(); request['footprints'][0]['directories'] = []
        self.blocked(request, 'storage_relation_unknown')
        request = fixture(); request['handles'][0]['before']['redirected'] = True
        request['handles'][0]['after'] = deepcopy(request['handles'][0]['before'])
        self.blocked(request, 'storage_relation_unknown')
        request = fixture(); child = deepcopy(request['handles'][0]); child['id'] = 'linked'
        child['before'].update(kind='file', root=child['before']['root'] + '/database', inode=99, links=2)
        child['after'] = deepcopy(child['before']); request['handles'].append(child)
        request['footprints'][0]['members'].append('linked')
        request['footprints'][0]['directories'][0]['children'].append('linked')
        self.blocked(request, 'storage_relation_unknown')

    def test_missing_sidecars_require_parent_directory_coverage(self):
        request = fixture(); state = next(s for s in request['states'] if s['category'] == 'sidecar_parent')
        state['coverage'] = 'subtree'
        self.blocked(request, 'state_parent_required')
        request = fixture(); h = request['handles'][0]; h['before']['kind'] = 'file'; h['before']['links'] = 1
        h['after'] = deepcopy(h['before']); request['footprints'][0]['directories'] = []
        self.blocked(request, 'state_parent_required')

    def test_transitive_credential_and_management_reach_owner_without_direct_mount(self):
        for kind in ['credential', 'management', 'recreate']:
            request = fixture()
            request['actors'] += [dict(id='outsider', service='', state='active'), dict(id='middle', service='', state='disabled')]
            request['edges'] += [dict(source='outsider', target='middle', kind=kind), dict(source='middle', target='a0', kind=kind)]
            self.blocked(request, 'indirect_writer_capability')

    def test_recreator_enables_disabled_writer_and_unknown_edges_block(self):
        request = fixture(); request['actors'].append(dict(id='old', service='', state='disabled'))
        request['edges'] += [dict(source='a1', target='old', kind='recreate'), dict(source='old', target='f0', kind='write')]
        self.blocked(request, 'competing_writer_or_recreator')
        request = fixture(); request['edges'].append(dict(source='a0', target='a1', kind='unknown'))
        self.blocked(request, 'unknown_capability_edge')
        request = fixture(); request['actors'][0]['state'] = 'unknown'
        self.blocked(request, 'unknown_actor_state')

    def test_owner_write_and_single_owner_coverage_cannot_be_omitted(self):
        request = fixture(); request['edges'].pop()
        self.blocked(request, 'owner_write_coverage_incomplete')
        request = fixture(); request['states'][0]['owner'] = 'a1'
        self.blocked(request, 'owner_unavailable')

    def test_reading_oauth_or_auth_state_is_a_credential_capability(self):
        for category in ['oauth_state', 'security_state', 'auth_state']:
            request = fixture(); state = next(s for s in request['states'] if s['category'] == category)
            request['actors'].append(dict(id='reader', service='', state='active'))
            request['edges'].append(dict(source='reader', target=state['footprint'], kind='read'))
            self.blocked(request, 'credential_read_capability')

    def test_expiry_runtime_deadline_and_nonfinite_clock_fail(self):
        for now in [11.0, float('nan'), True, 9.9]:
            self.blocked(fixture(), 'deadline_exceeded', now=now)
        request = fixture(); request['deadline'] = 13
        self.blocked(request, 'deadline_exceeded')
        ticks = iter([0.0, 0.81])
        self.blocked(fixture(), 'deadline_exceeded', clock=lambda: next(ticks))
        ticks = iter([1.0, 0.0])
        self.blocked(fixture(), 'invalid_clock', clock=lambda: next(ticks))

    def test_overflow_duplicate_and_secret_values_never_leak(self):
        request = fixture(); request['handles'] *= alias.ITEM_LIMIT
        self.blocked(request, 'payload_limit')
        request = fixture(); request['edges'] *= 300
        self.blocked(request, 'inventory_limit')
        request = fixture(); request['actors'].append(deepcopy(request['actors'][0]))
        self.blocked(request, 'duplicate_identity')
        request = fixture(); request['secret'] = 'PRIVATE_PATH_ACCOUNT_TOKEN'
        result = self.blocked(request)
        self.assertNotIn('PRIVATE', json.dumps(result))
        def unavailable_clock():
            raise alias.Rejected('PRIVATE_PROVIDER_EXCEPTION')
        result = self.blocked(fixture(), 'invalid_schema', clock=unavailable_clock)
        self.assertNotIn('PRIVATE', json.dumps(result))
        request = fixture(); request['handles'][0]['before']['root'] = '/state/../escape'
        self.blocked(request)

    def test_relation_work_is_bounded_independently_of_input_byte_cap(self):
        request = fixture()
        # A larger byte cap exists only in this fixture to reach the second,
        # independent algorithmic bound before the first unknown comparison.
        for group in range(2):
            root = request['handles'][group]
            for offset in range(250):
                h = deepcopy(root); h['id'] = 'child' + str(group) + '_' + str(offset)
                h['before'].update(kind='file', links=1, inode=1000 + group * 250 + offset,
                                   root=root['before']['root'] + '/child' + str(offset))
                h['after'] = deepcopy(h['before']); request['handles'].append(h)
                request['footprints'][group]['members'].append(h['id'])
                request['footprints'][group]['directories'][0]['children'].append(h['id'])
        with patch.object(alias, 'LIMIT', 1048576):
            self.blocked(request, 'analysis_limit')


if __name__ == '__main__':
    unittest.main()
