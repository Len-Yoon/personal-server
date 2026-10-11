"""Controller boundary tests; fake collectors never contact a live cluster."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout


SCRIPT = Path(__file__).resolve().parents[1] / 'infra/k8s/tools/fixed-ingress-rollout.py'
spec = importlib.util.spec_from_file_location('fixed_ingress_rollout', SCRIPT)
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)
NOW = datetime(2026, 10, 11, 1, 0, tzinfo=timezone.utc)


def candidate(snapshot, inventory, *, now):
    """Generator seam. The real fixed evidence schema has its own contract tests."""
    app = snapshot['metadata']['name']
    rollout.fresh(inventory['observed_at'], now)
    if inventory['existing_policies']:
        raise ValueError('existing policies')
    policy = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
        'metadata': {'name': app + '-ingress-reviewed', 'namespace': rollout.NS},
        'spec': {'podSelector': {'matchLabels': {'app.kubernetes.io/name': app}},
                 'policyTypes': ['Ingress'], 'ingress': [{'from': [inventory['caddy']['peer']],
                    'ports': [{'protocol': 'TCP', 'port': rollout.PORTS[app]}]}]}}
    return {'policy': policy, 'policy_sha256': rollout.digest(policy),
            'caddy_source_kind': 'fixed_pre_nat_ip', 'configured_fixed_address_proven': True}


class FakeBackend:
    target_commit = 'a' * 40

    def __init__(self):
        self.events = []
        self.objects = {}
        self.source = 'clean'
        self.protected = 'baseline'
        self.caddy = {'current_identity': {'container_id_sha256': 'b' * 64},
            'fixed_configuration': {'observed_at': NOW.isoformat(), 'runtime_config_sha256': 'c' * 64},
            'peer': {'ipBlock': {'cidr': '192.0.2.22/32'}}}
        self.collection_override = None
        self.probe_override = None
        self.create_override = None
        self.health_bad = False
        self.cleanup_ok = True
        self.available = True

    def journal(self, event):
        self.events.append(('journal', event['event']))

    def collector_available(self):
        return self.available

    def source_guard(self):
        self.events.append(('source',))
        rollout.require(self.source == 'clean', 'source_commit_drift')

    def guards(self):
        self.events.append(('guards',))
        return self.protected

    def identity(self):
        return deepcopy(self.caddy)

    def policies(self):
        return deepcopy(list(self.objects.values()))

    def current_policy(self, name):
        return deepcopy(self.objects.get(name))

    def collect(self, app):
        self.events.append(('collect', app))
        snapshot = {'metadata': {'name': app, 'uid': app + '-deployment'}, 'spec': {'image': 'sha'}}
        caddy = {**deepcopy(self.caddy), 'source_kind': 'fixed_pre_nat_ip',
                 'request_proof': {'port': rollout.PORTS[app],
                    'current_identity_sha256': rollout.digest(self.caddy['current_identity']),
                    'source_sha256': '1'*64, 'peer_sha256': rollout.digest(self.caddy['peer']),
                    'configuration_sha256': rollout.digest(self.caddy['fixed_configuration'])}}
        flows, exemptions = [], {}
        for category, required in (('fanout', app != 'portal-web'), ('metrics', app in ('portal-web', 'crawler-worker'))):
            if required:
                flows.append({'category': category, 'proof': {'source_pod_uid': category + '-pod'}})
            else:
                exemptions[category] = 'reviewed absence'
        inventory = {'source_commit': self.target_commit, 'observed_at': NOW.isoformat(),
            'cluster_uid': 'cluster-uid', 'target_node_uid': 'node-uid',
            'target_pod_uid': app + '-pod', 'target_service_uid': app + '-service',
            'target_service_spec_sha256': 'd' * 64, 'caddy': caddy, 'flows': flows,
            'target_endpoint_sha256': '3'*64,
            'not_applicable': exemptions, 'existing_policies': self.policies()}
        if self.collection_override:
            self.collection_override(app, snapshot, inventory)
        return snapshot, inventory

    def verify(self, app, policy_uid=None):
        self.events.append(('verify', app, policy_uid))
        probe = {'transport_ok': True, 'exit_code': 1 if policy_uid else 0,
                 'http_status': None if policy_uid else 200, 'reason': 'timeout' if policy_uid else 'none'}
        receipt = {'observed_at': NOW.isoformat(), 'policy_uid': policy_uid,
            'target_pod_uid': app + '-pod', 'target_service_uid': app + '-service',
            'target_service_spec_sha256': 'd' * 64,
            'current_identity_sha256': rollout.digest(self.caddy['current_identity']),
            'caddy_clusterip': {'status': 200, 'port': rollout.PORTS[app], 'same_connection_bound': True,
                'pid_start_inode_bound': True, 'http_marker_bound': True, 'route': 'ClusterIP',
                'proof_method': 'access_log_marker', 'server_endpoint_socket_bound': False,
                'source_matches_current_endpoint': True, 'target_endpoint_unchanged': True,
                'target_endpoint_sha256': '3'*64,
                'route_chosen_source_matches_fixed_address': True,
                'current_identity_sha256': rollout.digest(self.caddy['current_identity']),
                'source_sha256': '1'*64, 'peer_sha256': rollout.digest(self.caddy['peer']),
                'configuration_sha256': rollout.digest(self.caddy['fixed_configuration'])},
            'unknown': {'client_pod_uid': 'unknown-pod', 'client_ready_before': True,
                        'client_namespace_uid': 'namespace-uid', 'client_spec_sha256': '2'*64,
                        'client_ready_after': True, 'pod': deepcopy(probe), 'service': deepcopy(probe)},
            'fanout': {'status': 200, 'source_pod_uid': 'fanout-pod', 'direct_pod': True} if app != 'portal-web' else None,
            'metrics': {'status': 200, 'source_pod_uid': 'metrics-pod', 'post_nat_source_matches_pod': True}
                       if app in ('portal-web', 'crawler-worker') else None}
        if self.probe_override:
            self.probe_override(app, policy_uid, receipt)
        return receipt

    def create_policy(self, policy):
        app = policy['metadata']['name'].removesuffix('-ingress-reviewed')
        self.events.append(('create', app))
        actual = deepcopy(policy)
        actual['metadata'].update(uid=app + '-policy', resourceVersion='1')
        self.objects[actual['metadata']['name']] = actual
        if self.create_override:
            self.create_override(app, actual)
        return deepcopy(actual)

    def delete_policy(self, record, resource_version):
        self.events.append(('delete', record['name'], resource_version))
        rollout.check_owned(self.objects[record['name']], record)
        del self.objects[record['name']]

    def external_health(self, app):
        self.events.append(('health', app))
        return 503 if self.health_bad else 200

    def cleanup(self):
        self.events.append(('cleanup',))
        return self.cleanup_ok

    def begin_recovery(self):
        self.events.append(('recovery',))


class ControllerTests(unittest.TestCase):
    def controller(self, backend=None, generator=None):
        backend = backend or FakeBackend()
        sleep = Mock()
        obj = rollout.Controller(backend, generator=generator or candidate, now=lambda: NOW,
                                 sleep=sleep, run_id='e' * 32)
        return obj, backend, sleep

    def fail(self, controller, code):
        with self.assertRaisesRegex(rollout.Blocked, '^' + code + '$'):
            controller.run()
        self.assertEqual(controller.report['status'], 'FAIL')

    def test_four_targets_preflight_before_any_sequential_create(self):
        obj, backend, sleep = self.controller()
        result = obj.run()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['applied_count'], 4)
        self.assertTrue(result['owned_cleanup_complete'])
        self.assertFalse(result['caddy_only_identity_isolation'])
        self.assertEqual([e[1] for e in backend.events if e[0] == 'create'], list(rollout.ORDER))
        first = next(i for i, e in enumerate(backend.events) if e[0] == 'create')
        self.assertEqual({e[1] for e in backend.events[:first] if e[0] == 'collect'}, set(rollout.ORDER))
        self.assertEqual(sleep.call_count, 12)
        self.assertTrue(all(c.args == (10,) for c in sleep.call_args_list))
        for app in rollout.ORDER:
            self.assertEqual(sum(e == ('health', app) for e in backend.events), 18)
            probes = [e for e in backend.events if e[:2] == ('verify', app)]
            self.assertEqual(probes[0][2], None)
            self.assertEqual(probes[-1][2], app + '-policy')

    def test_missing_collector_cannot_create(self):
        obj, backend, _ = self.controller()
        backend.available = False
        self.fail(obj, 'trusted_native_collector_required')
        self.assertFalse(any(e[0] == 'create' for e in backend.events))

    def test_missing_durable_journal_cannot_create(self):
        obj, backend, _ = self.controller()
        backend.journal = None
        self.fail(obj, 'durable_journal_required')
        self.assertFalse(any(e[0] == 'create' for e in backend.events))

    def test_intent_is_durably_recorded_before_each_create(self):
        obj, backend, _ = self.controller()
        obj.run()
        for index, event in enumerate(backend.events):
            if event[0] == 'create':
                self.assertEqual(backend.events[index-1], ('journal', 'create_intent'))

    def test_real_generator_contract_integrates_for_each_of_four_ports(self):
        from tests import test_project_hardening_tools as contracts
        for app in rollout.ORDER:
            with self.subTest(app=app):
                module, snapshot, inventory = contracts.FixedPreNatIngressTests().fixed_fixture(app)
                backend = FakeBackend()
                backend.target_commit = inventory['source_commit']
                backend.collect = lambda a: (deepcopy(snapshot), deepcopy(inventory))
                obj = rollout.Controller(backend, generator=module.prepare)
                obj.binding = rollout.fixed_binding(inventory['caddy'])
                policy = obj.collect(app)
                self.assertEqual(policy['spec']['ingress'][0]['ports'][0]['port'], rollout.PORTS[app])
                self.assertEqual(policy['metadata']['name'], app + '-ingress-reviewed')

    def test_existing_policy_is_not_adopted_or_deleted(self):
        obj, backend, _ = self.controller()
        backend.objects['foreign'] = {'metadata': {'name': 'foreign'}, 'spec': {}}
        self.fail(obj, 'policy_inventory_drift')
        self.assertEqual(set(backend.objects), {'foreign'})
        self.assertFalse(any(e[0] == 'delete' for e in backend.events))

    def test_dynamic_source_or_wrong_port_blocks_every_production_write(self):
        for change, code in ((lambda i: i['caddy'].update(source_kind='pre_nat_exact_ip'), 'fixed_source_required'),
                             (lambda i: i['caddy']['request_proof'].update(port=True), 'fixed_route_port_required')):
            with self.subTest(code=code):
                obj, backend, _ = self.controller()
                backend.collection_override = lambda a, s, i: change(i) if a == 'portal-web' else None
                self.fail(obj, code)
                self.assertFalse(any(e[0] == 'create' for e in backend.events))

    def test_stale_observation_and_wrong_source_commit_are_rejected(self):
        for mutation, code in ((lambda i: i.update(observed_at=(NOW-timedelta(seconds=301)).isoformat()), 'fresh_observation_required'),
                               (lambda i: i.update(source_commit='f'*40), 'source_commit_binding_required')):
            with self.subTest(code=code):
                obj, backend, _ = self.controller()
                backend.collection_override = lambda a, s, i: mutation(i)
                self.fail(obj, code)
                self.assertFalse(backend.objects)

    def test_preflight_requires_same_fixed_identity_on_every_port(self):
        obj, backend, _ = self.controller()
        backend.collection_override = lambda a, s, i: i['caddy']['fixed_configuration'].update(runtime_config_sha256='f'*64) if a == 'crawler-worker' else None
        self.fail(obj, 'fixed_caddy_identity_drift')
        self.assertFalse(backend.objects)

    def test_all_four_routes_must_belong_to_same_current_cluster_and_node(self):
        obj, backend, _ = self.controller()
        backend.collection_override = lambda a, s, i: i.update(cluster_uid='foreign-cluster') if a == 'crawler-worker' else None
        self.fail(obj, 'cluster_node_identity_drift')
        self.assertFalse(backend.objects)

    def test_unknown_client_transport_failure_is_not_denial(self):
        obj, backend, _ = self.controller()
        backend.probe_override = lambda a, uid, r: r['unknown']['pod'].update(transport_ok=False) if uid else None
        self.fail(obj, 'unknown_probe_transport_failed')
        self.assertTrue(obj.report['rollback_complete'])
        self.assertFalse(backend.objects)

    def test_unknown_client_identity_must_match_successful_baseline(self):
        obj, backend, _ = self.controller()
        backend.probe_override = lambda a, uid, r: r['unknown'].update(client_pod_uid='replacement') if uid else None
        self.fail(obj, 'unknown_client_baseline_binding_required')
        self.assertFalse(backend.objects)

    def test_unknown_namespace_and_spec_cannot_change_after_baseline(self):
        for field, value in (('client_namespace_uid', 'other-ns'), ('client_spec_sha256', 'f'*64)):
            with self.subTest(field=field):
                obj, backend, _ = self.controller()
                backend.probe_override = lambda a, uid, r: r['unknown'].update({field: value}) if uid else None
                self.fail(obj, 'unknown_client_baseline_binding_required')
                self.assertFalse(backend.objects)

    def test_pod_and_service_denials_each_required(self):
        for route in ('pod', 'service'):
            with self.subTest(route=route):
                obj, backend, _ = self.controller()
                backend.probe_override = lambda a, uid, r: r['unknown'][route].update(exit_code=0, http_status=200) if uid else None
                self.fail(obj, 'unknown_route_not_denied')
                self.assertFalse(backend.objects)

    def test_target_uid_and_current_caddy_pid_inode_proof_required(self):
        for mutate, code in ((lambda r: r.update(target_pod_uid='wrong'), 'probe_target_binding_required'),
                             (lambda r: r['caddy_clusterip'].update(pid_start_inode_bound=False), 'current_caddy_clusterip_probe_required')):
            with self.subTest(code=code):
                obj, backend, _ = self.controller()
                backend.probe_override = lambda a, uid, r: mutate(r)
                self.fail(obj, code)
                self.assertFalse(backend.objects)

    def test_post_policy_caddy_cannot_use_node_route_or_another_source(self):
        for field, value in (('route', 'NodePort'), ('source_sha256', 'f'*64),
                             ('peer_sha256', 'f'*64), ('configuration_sha256', 'f'*64),
                             ('route_chosen_source_matches_fixed_address', False)):
            with self.subTest(field=field):
                obj, backend, _ = self.controller()
                backend.probe_override = lambda a, uid, r: r['caddy_clusterip'].update({field: value}) if uid else None
                self.fail(obj, 'current_caddy_fixed_route_binding_required')
                self.assertFalse(backend.objects)

    def test_explicit_socket_http_proof_supported_without_access_log_marker(self):
        obj, backend, _ = self.controller()
        backend.probe_override = lambda a, uid, r: r['caddy_clusterip'].update(
            proof_method='endpoint_socket_http', http_marker_bound=False, server_endpoint_socket_bound=True)
        self.assertEqual(obj.run()['status'], 'PASS')

    def test_socket_http_proof_cannot_claim_marker_or_foreign_endpoint(self):
        for changes in ({'proof_method': 'endpoint_socket_http', 'server_endpoint_socket_bound': True},
                        {'target_endpoint_sha256': 'f'*64}, {'proof_method': 'unknown'}):
            with self.subTest(changes=changes):
                obj, backend, _ = self.controller()
                backend.probe_override = lambda a, uid, r: r['caddy_clusterip'].update(changes)
                self.fail(obj, 'current_caddy_endpoint_proof_required')
                self.assertFalse(backend.objects)

    def test_portal_direct_fanout_and_prometheus_must_remain_allowed(self):
        for category, target in (('fanout', 'book-memo'), ('metrics', 'crawler-worker'), ('metrics', 'portal-web')):
            with self.subTest(category=category, target=target):
                obj, backend, _ = self.controller()
                backend.probe_override = lambda a, uid, r: r[category].update(status=503) if uid and a == target else None
                self.fail(obj, 'allowed_pod_flow_failed')
                self.assertFalse(backend.objects)
                removed = [e[1] for e in backend.events if e[0] == 'delete']
                created = [e[1] + '-ingress-reviewed' for e in backend.events if e[0] == 'create']
                self.assertEqual(removed, list(reversed(created)))

    def test_protected_drift_rolls_back_only_owned_policies(self):
        obj, backend, _ = self.controller()
        backend.create_override = lambda app, actual: setattr(backend, 'protected', 'drift') if app == 'youtube-memo' else None
        self.fail(obj, 'protected_state_drift')
        self.assertTrue(obj.report['rollback_complete'])
        self.assertFalse(obj.report['recovery_postguard_pass'])
        self.assertFalse(backend.objects)
        self.assertEqual([e[1] for e in backend.events if e[0] == 'delete'],
                         ['youtube-memo-ingress-reviewed', 'book-memo-ingress-reviewed'])

    def test_ambiguous_create_is_read_then_rolled_back_without_recreate(self):
        obj, backend, _ = self.controller()
        def timeout(app, actual):
            raise TimeoutError('must not leak input')
        backend.create_override = timeout
        self.fail(obj, 'operation_failed')
        self.assertEqual(sum(e[0] == 'create' for e in backend.events), 1)
        self.assertFalse(backend.objects)
        self.assertTrue(obj.report['rollback_complete'])
        self.assertTrue(obj.report['restored_flow_proven'])

    def test_absent_readback_cannot_resolve_attempted_create_or_release_recovery(self):
        obj, backend, _ = self.controller()
        def pending(app, actual):
            del backend.objects[actual['metadata']['name']]
            raise TimeoutError('pending API request')
        backend.create_override = pending
        self.fail(obj, 'operation_failed')
        self.assertFalse(obj.report['rollback_complete'])
        self.assertNotIn('restored_flow_proven', obj.report)
        self.assertEqual(len(obj.attempted_creates), 1)
        self.assertFalse(obj.observed_creates)
        self.assertEqual(sum(e[0] == 'create' for e in backend.events), 1)
        with self.assertRaisesRegex(rollout.Blocked, 'owned_policy_rollback_incomplete'):
            obj.rollback()
        self.assertEqual(sum(e[0] == 'create' for e in backend.events), 1)

    def test_late_exact_owned_create_can_be_observed_and_removed_without_recreate(self):
        obj, backend, _ = self.controller()
        pending = []
        def timeout(app, actual):
            pending.append(deepcopy(actual))
            del backend.objects[actual['metadata']['name']]
            raise TimeoutError('pending API request')
        backend.create_override = timeout
        self.fail(obj, 'operation_failed')
        actual = pending[0]
        backend.objects[actual['metadata']['name']] = actual
        obj.rollback()  # Explicit reviewed recovery, never another create.
        self.assertTrue(obj.report['rollback_complete'])
        self.assertFalse(backend.objects)
        self.assertEqual(sum(e[0] == 'create' for e in backend.events), 1)
        self.assertEqual(sum(e[0] == 'delete' for e in backend.events), 1)

    def test_prewrite_create_rejection_and_journal_failure_are_known_not_attempted(self):
        for stage in ('backend_validation', 'intent_journal'):
            with self.subTest(stage=stage):
                obj, backend, _ = self.controller()
                if stage == 'backend_validation':
                    backend.create_policy = Mock(side_effect=rollout.CreateNotAttempted('invalid_policy_create'))
                    code = 'invalid_policy_create'
                else:
                    original = backend.journal
                    def failed(event):
                        if event['event'] == 'create_intent':
                            raise rollout.Blocked('durable_journal_failed')
                        original(event)
                    backend.journal = failed
                    code = 'durable_journal_failed'
                self.fail(obj, code)
                self.assertFalse(obj.attempted_creates)
                self.assertTrue(obj.report['rollback_complete'])
                self.assertFalse(backend.objects)

    def test_pending_second_create_does_not_prevent_first_owned_policy_cleanup(self):
        obj, backend, _ = self.controller()
        def pending(app, actual):
            if app == 'youtube-memo':
                del backend.objects[actual['metadata']['name']]
                raise TimeoutError('pending API request')
        backend.create_override = pending
        self.fail(obj, 'operation_failed')
        self.assertFalse(obj.report['rollback_complete'])
        self.assertFalse(backend.objects)
        self.assertEqual([e[1] for e in backend.events if e[0] == 'delete'], ['book-memo-ingress-reviewed'])

    def test_replaced_uid_or_modified_spec_cannot_be_rolled_back(self):
        for mutate in (lambda p: p['metadata'].update(uid='replacement'), lambda p: p['spec'].update(egress=[])):
            with self.subTest(mutate=mutate):
                obj, backend, _ = self.controller()
                def break_probe(app, uid, receipt):
                    if uid:
                        mutate(backend.objects[app + '-ingress-reviewed'])
                        receipt['unknown']['pod']['transport_ok'] = False
                backend.probe_override = break_probe
                self.fail(obj, 'unknown_probe_transport_failed')
                self.assertFalse(obj.report['rollback_complete'])
                self.assertTrue(backend.objects)
                self.assertFalse(any(e[0] == 'delete' for e in backend.events))

    def test_foreign_replacement_does_not_prevent_other_owned_policy_cleanup(self):
        obj, backend, _ = self.controller()
        def replace_second(app, uid, receipt):
            if uid and app == 'youtube-memo':
                backend.objects[app + '-ingress-reviewed']['metadata']['uid'] = 'foreign'
                receipt['unknown']['pod']['transport_ok'] = False
        backend.probe_override = replace_second
        self.fail(obj, 'unknown_probe_transport_failed')
        self.assertFalse(obj.report['rollback_complete'])
        self.assertEqual(set(backend.objects), {'youtube-memo-ingress-reviewed'})
        self.assertEqual([e[1] for e in backend.events if e[0] == 'delete'], ['book-memo-ingress-reviewed'])

    def test_cleanup_failure_rolls_back_successful_policies(self):
        obj, backend, _ = self.controller()
        backend.cleanup_ok = False
        self.fail(obj, 'owned_collector_cleanup_incomplete')
        self.assertFalse(obj.report['owned_cleanup_complete'])
        self.assertTrue(obj.report['rollback_complete'])
        self.assertFalse(backend.objects)

    def test_public_health_failure_before_any_write(self):
        obj, backend, _ = self.controller()
        backend.health_bad = True
        self.fail(obj, 'external_health_failed')
        self.assertFalse(any(e[0] == 'create' for e in backend.events))


class NativeBoundaryTests(unittest.TestCase):
    def backend(self):
        return rollout.NativeBackend(SCRIPT.parents[3], 'a' * 40, journal=Mock())

    def policy(self):
        p = candidate({'metadata': {'name': 'book-memo'}},
                      {'observed_at': NOW.isoformat(), 'existing_policies': [],
                       'caddy': {'peer': {'ipBlock': {'cidr': '192.0.2.22/32'}}}}, now=NOW)['policy']
        p['metadata']['labels'] = {rollout.OWNER: 'e'*32}
        p['metadata'].update(uid='policy-uid', resourceVersion='3')
        return p

    def test_cli_does_not_accept_proof_file_or_execute_operations(self):
        with patch.object(rollout.NativeBackend, 'run', side_effect=AssertionError('unexpected operation')):
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(rollout.main([]), 2)
                self.assertEqual(rollout.main(['--describe']), 0)
            self.assertIn('trusted_native_collector_required', out.getvalue())

    def test_secret_reads_request_metadata_only_for_list_and_single_resource(self):
        for kind, name in (('secrets', None), ('secret', 'one')):
            with self.subTest(kind=kind):
                backend = self.backend()
                backend.run = Mock(return_value=b'one uid rv Opaque\n')
                actual = backend.get(kind, rollout.NS, name)
                args = backend.run.call_args.args[0]
                self.assertTrue(any(a.startswith('-o=custom-columns=NAME:') for a in args))
                self.assertNotIn('json', args)
                self.assertNotIn('"data"', json.dumps(actual))

    def test_native_delete_uses_uid_and_latest_resource_version(self):
        backend, policy = self.backend(), self.policy()
        backend.current_policy = Mock(side_effect=[policy, None])
        backend.run = Mock(return_value=b'{}')
        record = rollout.policy_record(policy, 'e'*32)
        backend.delete_policy(record, '3')
        args, kwargs = backend.run.call_args.args[0], backend.run.call_args.kwargs
        self.assertIn('--raw', args)
        self.assertEqual(kwargs['data']['preconditions'], {'uid': 'policy-uid', 'resourceVersion': '3'})

    def test_delete_resource_version_drift_stops_before_write(self):
        backend, policy = self.backend(), self.policy()
        backend.current_policy = Mock(return_value=policy)
        backend.run = Mock()
        with self.assertRaisesRegex(rollout.Blocked, 'policy_resource_version_drift'):
            backend.delete_policy(rollout.policy_record(policy, 'e'*32), 'old')
        backend.run.assert_not_called()

    def test_existing_policy_never_gets_apply_patch_or_replace(self):
        backend, policy = self.backend(), self.policy()
        backend.current_policy = Mock(return_value=policy)
        backend.run = Mock()
        with self.assertRaisesRegex(rollout.Blocked, 'existing_policy_not_overwritten'):
            backend.create_policy(policy)
        backend.run.assert_not_called()

    def test_native_validation_and_journal_failure_precede_any_create_request(self):
        backend, policy = self.backend(), self.policy()
        backend.current_policy = Mock(return_value=None)
        backend.run = Mock()
        backend.journal.side_effect = OSError('private filesystem diagnostic')
        with self.assertRaisesRegex(rollout.CreateNotAttempted, 'policy_create_not_attempted'):
            backend.create_policy(policy)
        backend.run.assert_not_called()

    def test_native_started_create_failure_does_not_claim_not_attempted(self):
        backend, policy = self.backend(), self.policy()
        backend.current_policy = Mock(return_value=None)
        backend.run = Mock(side_effect=rollout.Blocked('native_command_incomplete'))
        with self.assertRaises(rollout.Blocked) as caught:
            backend.create_policy(policy)
        self.assertNotIsInstance(caught.exception, rollout.CreateNotAttempted)

    def test_git_child_uses_isolated_configuration_without_mutating_parent(self):
        backend = self.backend()
        child = Mock(pid=123, returncode=0)
        child.communicate.return_value = (b'readonly', None)
        parent = {'SYNTHETIC_PARENT': 'preserved', 'GIT_DIR': '/synthetic/other',
                  'GIT_CONFIG_COUNT': '1', 'GIT_TRACE': '/synthetic/private'}
        with patch.object(rollout.subprocess, 'Popen', return_value=child) as popen, \
             patch.object(rollout.os, 'killpg', side_effect=ProcessLookupError()), \
             patch.dict(os.environ, parent, clear=True):
            self.assertEqual(backend.run(['git', '--no-optional-locks', '--no-replace-objects', 'status']), b'readonly')
            self.assertEqual(dict(os.environ), parent)
        self.assertEqual(popen.call_args.args[0][:3], ['git', '-c', 'core.attributesFile=/dev/null'])
        self.assertEqual(popen.call_args.kwargs['env'], {'SYNTHETIC_PARENT': 'preserved',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_ATTR_NOSYSTEM': '1'})

    def test_git_external_settings_are_rejected_before_any_status_or_include_read(self):
        entries = ('include.path\n/synthetic/private', 'includeIf.gitdir:/x/.path\n/synthetic/private',
            'filter.synthetic.process\nprivate-command', 'filter.synthetic.clean\nprivate-command',
            'core.attributesFile\n/synthetic/private', 'core.sshCommand\nprivate-command',
            'diff.synthetic.command\nprivate-command', 'core.fsmonitor\nprivate-command',
            'core.hooksPath\n/synthetic/private')
        for entry in entries:
            with self.subTest(key=entry.split('\n')[0]), patch.dict(os.environ, {}, clear=True):
                backend = self.backend()
                backend.run = Mock(return_value=(entry+'\0core.attributesfile\n/dev/null\0').encode())
                with self.assertRaisesRegex(rollout.Blocked, 'unsafe_git_configuration'):
                    backend.source_guard()
                self.assertEqual(backend.run.call_count, 1)
                self.assertEqual(backend.run.call_args.args[0][3:], ['config', '--no-includes', '--null', '--list'])

    def test_git_parent_environment_override_stops_before_command(self):
        backend = self.backend()
        backend.run = Mock()
        with patch.dict(os.environ, {'GIT_DIR': '/synthetic/other'}, clear=True):
            with self.assertRaisesRegex(rollout.Blocked, 'git_environment_override'):
                backend.source_guard()
        backend.run.assert_not_called()

    def test_parent_exit_with_surviving_group_is_terminated(self):
        backend = self.backend()
        child = Mock(pid=123, returncode=0)
        child.communicate.side_effect = subprocess.TimeoutExpired(['bounded'], 1)
        child.poll.return_value = 0
        with patch.object(rollout.subprocess, 'Popen', return_value=child), \
             patch.object(rollout.os, 'killpg', side_effect=[None, None, None, ProcessLookupError()]) as kill:
            with self.assertRaisesRegex(rollout.Blocked, 'native_command_incomplete'):
                backend.run(['bounded'])
        self.assertEqual(kill.call_args_list[1].args, (123, signal.SIGTERM))
        self.assertEqual(kill.call_args_list[2].args, (123, signal.SIGKILL))
        self.assertFalse(backend.termination_unknown)

    def test_unproven_group_termination_blocks_following_command(self):
        backend = self.backend()
        child = Mock(pid=123, returncode=0)
        child.communicate.side_effect = subprocess.TimeoutExpired(['bounded'], 1)
        with patch.object(rollout.subprocess, 'Popen', return_value=child), \
             patch.object(rollout.os, 'killpg', side_effect=PermissionError()):
            with self.assertRaises(rollout.Blocked):
                backend.run(['bounded'])
        self.assertTrue(backend.termination_unknown)
        with self.assertRaisesRegex(rollout.Blocked, 'child_termination_unknown'):
            backend.run(['never'])

    def test_health_uses_fixed_public_url_and_rejects_redirect(self):
        backend = self.backend()
        response = Mock(status=200)
        response.geturl.return_value = 'https://example.invalid/health'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(rollout.urllib.request, 'urlopen', return_value=response) as opened:
            with self.assertRaisesRegex(rollout.Blocked, 'external_health_redirect'):
                backend.external_health('portal-web')
        self.assertEqual(opened.call_args.args[0].full_url, 'https://len.pe.kr/health')

    def test_recovery_budget_is_separate_and_bounded(self):
        backend = self.backend()
        backend.deadline = 0
        with patch.object(rollout.time, 'monotonic', return_value=100):
            backend.begin_recovery()
        self.assertEqual(backend.deadline, 400)


if __name__ == '__main__':
    unittest.main()
