#!/usr/bin/env python3
"""Separately authorized, source-first fixed Caddy ingress rollout.

The generator is deliberately not an operating authority. This controller adds
ordered create-only production admission and verification. A reviewed native
collector must be explicitly supplied through the Python API; JSON files or
arbitrary modules cannot act as proof collectors. No collector is shipped or
implicitly selected here. The CLI consequently performs no operations.

The caller holds the existing backup-sequence lock across Caddy transition and
this entire operation. This module never acquires a second lock, changes Caddy,
restarts applications, patches existing policies, or writes sensitive evidence.
Collector evidence stays in memory. Results contain only counts and hashes.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import urllib.request


ORDER = ('book-memo', 'youtube-memo', 'crawler-worker', 'portal-web')
PORTS = {'book-memo': 8003, 'youtube-memo': 8002, 'crawler-worker': 8001, 'portal-web': 8000}
HEALTH = {'book-memo': 'https://books.len.pe.kr/health',
          'youtube-memo': 'https://memo.len.pe.kr/health',
          'crawler-worker': 'https://news.len.pe.kr/health',
          'portal-web': 'https://len.pe.kr/health'}
NS = 'personal-server'
OWNER = 'len.pe.kr/fixed-ingress-run'
KUBE = ['sudo', '-n', 'k3s', 'kubectl', '--request-timeout=15s']


class Blocked(Exception):
    """Only constant, non-sensitive reason codes cross the reporting boundary."""


class CreateNotAttempted(Blocked):
    """Trusted backend rejected a create before starting the API command."""


def require(condition, code):
    if not condition:
        raise Blocked(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def load_generator():
    spec = importlib.util.spec_from_file_location('fixed_ingress_generator',
        Path(__file__).with_name('prepare-ingress-networkpolicy.py'))
    module = importlib.util.module_from_spec(spec)
    source = Path(__file__).with_name('prepare-ingress-networkpolicy.py')
    exec(compile(source.read_bytes(), str(source), 'exec'), module.__dict__)
    return module.prepare


def fresh(value, now):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(stamp.tzinfo is not None and 0 <= (now - stamp).total_seconds() <= 300,
                'fresh_observation_required')
    except (AttributeError, TypeError, ValueError):
        raise Blocked('fresh_observation_required') from None


def fixed_binding(caddy):
    """Ignore collection timestamp only; all configured identity claims bind."""
    config = deepcopy(caddy['fixed_configuration'])
    config.pop('observed_at', None)
    return digest({'current_identity': caddy['current_identity'],
                   'fixed_configuration': config, 'peer': caddy['peer']})


def policy_record(obj, run_id):
    meta = obj.get('metadata', {})
    require(meta.get('namespace') == NS and meta.get('name') in
            {a + '-ingress-reviewed' for a in ORDER}, 'unexpected_policy_target')
    require(meta.get('uid') and meta.get('resourceVersion') and
            meta.get('labels', {}).get(OWNER) == run_id and not meta.get('deletionTimestamp'),
            'policy_ownership_required')
    return {'name': meta['name'], 'uid': meta['uid'], 'spec_sha256': digest(obj['spec']), 'run_id': run_id}


def check_owned(obj, record):
    require(obj is not None and policy_record(obj, record['run_id']) == record,
            'owned_policy_precondition_failed')


def check_writer(deployment, pods):
    spec = deployment.get('spec', {})
    status = deployment.get('status', {})
    meta = deployment.get('metadata', {})
    require(type(spec.get('replicas')) is int and spec['replicas'] == 1 and
            spec.get('strategy', {}).get('type') == 'Recreate' and
            status.get('readyReplicas') == status.get('availableReplicas') == 1 and
            status.get('observedGeneration', 0) >= meta.get('generation', 1) and
            not meta.get('deletionTimestamp'), 'single_ready_writer_required')
    labels = spec.get('selector', {}).get('matchLabels', {})
    require(bool(labels), 'writer_selector_required')
    selected = [p for p in pods if all(p.get('metadata', {}).get('labels', {}).get(k) == v
                for k, v in labels.items()) and p.get('status', {}).get('phase') not in ('Succeeded', 'Failed')]
    require(len(selected) == 1, 'single_writer_pod_required')
    pod = selected[0]
    containers = pod.get('status', {}).get('containerStatuses', [])
    require(not pod.get('metadata', {}).get('deletionTimestamp') and
            pod.get('status', {}).get('phase') == 'Running' and
            any(c.get('type') == 'Ready' and c.get('status') == 'True'
                for c in pod['status'].get('conditions', [])) and len(containers) == 1 and
            all(c.get('ready') is True and type(c.get('restartCount')) is int and
                c['restartCount'] == 0 and c.get('state', {}).get('running') and
                not c.get('lastState', {}).get('terminated') for c in containers), 'writer_restart_or_unready')
    declared = spec.get('template', {}).get('spec', {}).get('containers', [])
    require(len(declared) == 1 and declared[0].get('image') == containers[0].get('imageID') and
            re.fullmatch(r'.+@sha256:[a-f0-9]{64}', declared[0]['image']), 'writer_image_digest_required')
    return pod


class Controller:
    """Injectable orchestration; every write has source/guard/ownership gates."""

    def __init__(self, backend, *, generator=None, now=None, sleep=None, run_id=None):
        self.backend = backend
        self.generator = generator or load_generator()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or time.sleep
        self.run_id = run_id or os.urandom(16).hex()
        require(bool(re.fullmatch('[a-f0-9]{32}', self.run_id)), 'run_id_required')
        self.owned = []
        self.inventories = {}
        self.snapshots = {}
        self.baseline = None
        self.binding = None
        self.cluster_binding = None
        self.intents = []
        self.attempted_creates = set()
        self.observed_creates = set()
        self.clients = {}
        self.report = {'status': 'NOT_STARTED', 'applied_count': 0,
                       'rollback_complete': False, 'owned_cleanup_complete': False,
                       'caddy_only_identity_isolation': False, 'egress_unchanged': True}

    def gate(self):
        self.backend.source_guard()
        current = self.backend.guards()
        if self.baseline is not None:
            require(current == self.baseline, 'protected_state_drift')
        if self.binding is not None:
            require(fixed_binding(self.backend.identity()) == self.binding, 'fixed_caddy_identity_drift')
        actual = self.backend.policies()
        require(len(actual) == len(self.owned), 'policy_inventory_drift')
        names = {obj.get('metadata', {}).get('name'): obj for obj in actual}
        require(len(names) == len(actual), 'policy_inventory_drift')
        for record in self.owned:
            check_owned(names.get(record['name']), record)
        return current

    def journal(self, event, **values):
        require(callable(getattr(self.backend, 'journal', None)), 'durable_journal_required')
        self.backend.journal({'event': event, 'run_id': self.run_id,
            'source_commit': self.backend.target_commit, 'observed_at': self.now().isoformat(), **values})

    def collect(self, app):
        snapshot, inventory = self.backend.collect(app)
        require(inventory.get('source_commit') == self.backend.target_commit,
                'source_commit_binding_required')
        cluster = (inventory.get('cluster_uid'), inventory.get('target_node_uid'))
        require(all(isinstance(value, str) and value for value in cluster), 'cluster_node_binding_required')
        if self.cluster_binding is None:
            self.cluster_binding = cluster
        require(cluster == self.cluster_binding, 'cluster_node_identity_drift')
        caddy = inventory.get('caddy', {})
        require(caddy.get('source_kind') == 'fixed_pre_nat_ip', 'fixed_source_required')
        require(type(caddy.get('request_proof', {}).get('port')) is int and
                caddy['request_proof']['port'] == PORTS[app], 'fixed_route_port_required')
        require(fixed_binding(caddy) == self.binding, 'fixed_caddy_identity_drift')
        if app in self.snapshots:
            previous = self.snapshots[app]
            require(snapshot.get('metadata', {}).get('uid') == previous.get('metadata', {}).get('uid') and
                    digest(snapshot.get('spec')) == digest(previous.get('spec')), 'target_snapshot_drift')
        # The prepare-only generator requires the target to have no policies.
        # Keep its actual inventory intact; remove only policies already admitted
        # for other apps by this controller, after the complete union was checked.
        inventory = deepcopy(inventory)
        require(inventory.get('existing_policies') == self.backend.policies(), 'collector_policy_inventory_drift')
        inventory['existing_policies'] = []
        try:
            candidate = self.generator(snapshot, inventory, now=self.now())
        except (ValueError, KeyError, TypeError):
            raise Blocked('fixed_generator_rejected') from None
        policy = candidate.get('policy', {})
        require(candidate.get('caddy_source_kind') == 'fixed_pre_nat_ip' and
                candidate.get('configured_fixed_address_proven') is True and
                candidate.get('policy_sha256') == digest(policy), 'fixed_generator_contract_required')
        require(policy.get('metadata') == {'name': app + '-ingress-reviewed', 'namespace': NS} and
                policy.get('kind') == 'NetworkPolicy' and policy.get('apiVersion') == 'networking.k8s.io/v1' and
                policy.get('spec', {}).get('podSelector') == {'matchLabels': {'app.kubernetes.io/name': app}} and
                policy['spec'].get('policyTypes') == ['Ingress'] and 'egress' not in policy['spec'],
                'ingress_only_policy_required')
        self.snapshots[app], self.inventories[app] = deepcopy(snapshot), inventory
        return deepcopy(policy)

    def verify(self, app, policy_uid=None):
        receipt = self.backend.verify(app, policy_uid)
        inventory = self.inventories[app]
        fresh(receipt.get('observed_at'), self.now())
        for key in ('target_pod_uid', 'target_service_uid', 'target_service_spec_sha256'):
            require(receipt.get(key) == inventory.get(key) and bool(inventory.get(key)), 'probe_target_binding_required')
        require(receipt.get('policy_uid') == policy_uid and
                receipt.get('current_identity_sha256') == digest(inventory['caddy']['current_identity']),
                'probe_policy_identity_binding_required')
        caddy = receipt.get('caddy_clusterip', {})
        require(type(caddy.get('status')) is int and caddy['status'] == 200 and
                type(caddy.get('port')) is int and caddy['port'] == PORTS[app] and
                all(caddy.get(k) is True for k in ('same_connection_bound', 'pid_start_inode_bound',
                    'source_matches_current_endpoint', 'target_endpoint_unchanged')),
                'current_caddy_clusterip_probe_required')
        method = caddy.get('proof_method')
        endpoint_sha = inventory.get('target_endpoint_sha256')
        require(method in ('access_log_marker', 'endpoint_socket_http') and
                caddy.get('http_marker_bound') is (method == 'access_log_marker') and
                caddy.get('server_endpoint_socket_bound') is (method == 'endpoint_socket_http') and
                isinstance(endpoint_sha, str) and re.fullmatch('[a-f0-9]{64}', endpoint_sha) and
                caddy.get('target_endpoint_sha256') == endpoint_sha,
                'current_caddy_endpoint_proof_required')
        proof = inventory['caddy']['request_proof']
        require(caddy.get('route') == 'ClusterIP' and
                caddy.get('route_chosen_source_matches_fixed_address') is True and
                all(caddy.get(k) == proof.get(k) and bool(proof.get(k)) for k in
                    ('current_identity_sha256', 'source_sha256', 'peer_sha256', 'configuration_sha256')),
                'current_caddy_fixed_route_binding_required')
        unknown = receipt.get('unknown', {})
        require(bool(unknown.get('client_pod_uid')) and unknown.get('client_ready_before') is True and
                unknown.get('client_ready_after') is True, 'healthy_unknown_client_required')
        client = {k: unknown.get(k) for k in ('client_pod_uid', 'client_namespace_uid', 'client_spec_sha256')}
        require(bool(client['client_namespace_uid']) and isinstance(client['client_spec_sha256'], str) and
                re.fullmatch('[a-f0-9]{64}', client['client_spec_sha256']), 'unknown_client_identity_required')
        if policy_uid:
            require(self.clients.get(app) == client, 'unknown_client_baseline_binding_required')
        else:
            self.clients[app] = client
        for route in ('pod', 'service'):
            probe = unknown.get(route, {})
            require(probe.get('transport_ok') is True and type(probe.get('exit_code')) is int,
                    'unknown_probe_transport_failed')
            if policy_uid:
                require(probe['exit_code'] == 1 and probe.get('http_status') is None and
                        probe.get('reason') in ('timeout', 'refused', 'unreachable'), 'unknown_route_not_denied')
            else:
                require(probe['exit_code'] == 0 and type(probe.get('http_status')) is int and
                        probe['http_status'] == 200 and probe.get('reason') == 'none', 'unknown_baseline_not_allowed')
        for category in ('fanout', 'metrics'):
            flow = next((f for f in inventory['flows'] if f['category'] == category), None)
            probe = receipt.get(category)
            if flow is None:
                require(probe is None and category in inventory['not_applicable'], 'unexpected_probe_exemption')
            else:
                require(isinstance(probe, dict) and type(probe.get('status')) is int and probe['status'] == 200 and
                        probe.get('source_pod_uid') == flow['proof']['source_pod_uid'] and
                        probe.get('direct_pod' if category == 'fanout' else 'post_nat_source_matches_pod') is True,
                        'allowed_pod_flow_failed')
        return digest(receipt)

    def health(self):
        for round_number in range(3):
            for app in ORDER:
                require(self.backend.external_health(app) == 200, 'external_health_failed')
            if round_number < 2:
                self.sleep(10)

    def reconcile_intents(self):
        """An absent GET cannot cancel an already submitted create request."""
        unresolved = False
        for intent in self.intents:
            name = intent['metadata']['name']
            if name not in self.attempted_creates or name in self.observed_creates:
                continue
            obj = self.backend.current_policy(name)
            if obj is not None:
                record = policy_record(obj, self.run_id)
                require(record['spec_sha256'] == digest(intent['spec']), 'ambiguous_create_spec_drift')
                self.owned.append(record)
                self.observed_creates.add(name)
                self.journal('create_reconciled', policy=record)
            else:
                unresolved = True
        require(not unresolved, 'policy_create_response_unresolved')

    def rollback(self):
        # Protective drift must not prevent removal of our own restrictive
        # policies. Rollback cannot mutate Caddy or any protected resource.
        self.report['rollback_complete'] = False
        if callable(getattr(self.backend, 'begin_recovery', None)):
            self.backend.begin_recovery()
        self.journal('recovery_started', policy_count=len(self.owned))
        failed = False
        try:
            self.reconcile_intents()
        except Exception:
            failed = True
        for record in reversed(self.owned[:]):
            try:
                obj = self.backend.current_policy(record['name'])
                if obj is not None:
                    check_owned(obj, record)
                    self.backend.delete_policy(record, obj['metadata']['resourceVersion'])
                    require(self.backend.current_policy(record['name']) is None, 'owned_policy_delete_incomplete')
                self.owned.remove(record)
                self.journal('delete_confirmed', policy=record)
            except Exception:
                # A replaced policy is held without preventing cleanup of
                # independent policies whose ownership is still provable.
                failed = True
        require(not failed and not self.owned, 'owned_policy_rollback_incomplete')
        self.report['rollback_complete'] = True

    def run(self):
        self.report['status'] = 'RUNNING'
        try:
            require(self.backend.collector_available(), 'trusted_native_collector_required')
            self.journal('controller_started')
            self.baseline = self.gate()
            self.binding = fixed_binding(self.backend.identity())
            # Establish every current route and isolated causal proof before the
            # first production policy, then recollect each just before admission.
            for app in ORDER:
                self.collect(app)
                self.gate()
            self.health()
            for app in ORDER:
                self.gate()
                policy = self.collect(app)
                self.verify(app)
                self.gate()
                policy['metadata']['labels'] = {OWNER: self.run_id}
                self.intents.append(deepcopy(policy))
                self.journal('create_intent', name=policy['metadata']['name'], spec_sha256=digest(policy['spec']))
                name = policy['metadata']['name']
                self.attempted_creates.add(name)
                try:
                    actual = self.backend.create_policy(policy)
                except CreateNotAttempted:
                    self.attempted_creates.discard(name)
                    self.journal('create_not_attempted', name=name)
                    raise
                record = policy_record(actual, self.run_id)
                require(record['spec_sha256'] == digest(policy['spec']), 'created_policy_spec_drift')
                self.owned.append(record)
                self.observed_creates.add(name)
                self.journal('create_confirmed', policy=record)
                self.gate()
                self.verify(app, record['uid'])
                self.health()
                self.gate()
                self.report['applied_count'] += 1
            for record in self.owned:
                self.verify(record['name'].removesuffix('-ingress-reviewed'), record['uid'])
            self.health()
            # Cleanup failure is a rollout failure; do not report partial success.
            require(self.backend.cleanup() is True, 'owned_collector_cleanup_incomplete')
            self.report['owned_cleanup_complete'] = True
            self.gate()
            self.report.update(status='PASS', policies_sha256=digest(self.owned))
            self.journal('controller_finished', result=self.report)
            return deepcopy(self.report)
        except BaseException as exc:
            code = str(exc) if isinstance(exc, Blocked) else 'operation_failed'
            self.report.update(status='FAIL', reason=code if re.fullmatch('[a-z0-9_]{1,80}', code) else 'operation_failed')
            try:
                self.rollback()
            except BaseException:
                self.report['rollback_complete'] = False
            try:
                if self.report['rollback_complete']:
                    for intent in self.intents:
                        self.verify(intent['metadata']['name'].removesuffix('-ingress-reviewed'))
                    self.report['restored_flow_proven'] = True
            except BaseException:
                self.report['restored_flow_proven'] = False
            try:
                self.report['owned_cleanup_complete'] = self.backend.cleanup() is True
            except BaseException:
                self.report['owned_cleanup_complete'] = False
            try:
                self.gate()
                self.health()
                self.report['recovery_postguard_pass'] = True
            except BaseException:
                self.report['recovery_postguard_pass'] = False
            try:
                self.journal('controller_finished', result=self.report)
            except BaseException:
                self.report['journal_complete'] = False
            raise Blocked(self.report['reason']) from None


class NativeBackend:
    """Bounded local kube client, protected guards and owned-policy mutations.

    collector is trusted executable code selected by the reviewed parent adapter,
    never an import path from input. It owns isolated probes and their UID/spec
    cleanup; production policies are exclusively owned by this backend.

    journal(event_dict) is a required synchronous, fsynced callback for writes.
    The parent creates its private append-only run ledger before calling run(),
    refuses unfinished prior runs, and reconciles journaled intents after a hard
    process interruption. This module's in-memory recovery covers ordinary
    failures; it does not independently implement SIGKILL/restart recovery.
    """

    def __init__(self, root, target_commit, collector=None, journal=None):
        self.root = Path(root).resolve()
        require(bool(re.fullmatch('[a-f0-9]{40}', target_commit)), 'exact_source_commit_required')
        self.target_commit = target_commit
        self.collector = collector
        self.journal = journal
        self.termination_unknown = False
        self.deadline = time.monotonic() + 1800

    def begin_recovery(self):
        # Reserve a separate bounded window for rollback after a forward timeout.
        self.deadline = time.monotonic() + 300

    def collector_available(self):
        return self.collector is not None and all(callable(getattr(self.collector, name, None))
            for name in ('identity', 'collect', 'verify', 'cleanup'))

    def run(self, args, *, data=None, timeout=30):
        require(not self.termination_unknown, 'child_termination_unknown')
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, 'operation_budget_exhausted')
        child = None
        try:
            env = None
            if args[0] == 'git':
                env = {**{k: v for k, v in os.environ.items() if not k.startswith('GIT_')}, 'GIT_CONFIG_NOSYSTEM': '1',
                       'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_ATTR_NOSYSTEM': '1'}
                args = [args[0], '-c', 'core.attributesFile=/dev/null', *args[1:]]
            child = subprocess.Popen(args, cwd=self.root, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, env=env)
            output, _ = child.communicate(canonical(data) if data is not None else None, timeout=min(timeout, remaining))
            require(child.returncode == 0, 'native_command_failed')
            return output
        except (OSError, subprocess.TimeoutExpired):
            raise Blocked('native_command_incomplete') from None
        finally:
            if child is not None:
                # The direct child may have exited while a descendant still
                # holds stdout or can finish a privileged write. Check the
                # complete process group regardless of the parent's status.
                group_alive = True
                try:
                    os.killpg(child.pid, 0)
                except ProcessLookupError:
                    group_alive = False
                except PermissionError:
                    self.termination_unknown = True
                if group_alive:
                    for sig in (signal.SIGTERM, signal.SIGKILL):
                        try:
                            os.killpg(child.pid, sig)
                        except ProcessLookupError:
                            pass
                        except PermissionError:
                            self.termination_unknown = True
                        try:
                            child.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            self.termination_unknown = True
                    try:
                        os.killpg(child.pid, 0)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        self.termination_unknown = True
                    else:
                        self.termination_unknown = True
                for stream in (child.stdin, child.stdout):
                    if stream:
                        stream.close()

    def get(self, kind, namespace, name=None):
        args = KUBE + ['-n', namespace, 'get', kind] + ([name] if name else [])
        if kind in ('secret', 'secrets'):
            # Secret payloads never cross the command boundary, even in memory.
            raw = self.run(args + ['-o=custom-columns=NAME:.metadata.name,UID:.metadata.uid,RV:.metadata.resourceVersion,TYPE:.type', '--no-headers'])
            rows = [line.split() for line in raw.decode().splitlines()]
            require(all(len(row) == 4 and '<none>' not in row for row in rows), 'secret_metadata_required')
            values = [{'metadata': dict(zip(('name', 'uid', 'resourceVersion'), r[:3])), 'type': r[3]} for r in rows]
            if name:
                require(len(values) == 1, 'secret_metadata_required')
                return values[0]
            return {'items': values}
        try:
            return json.loads(self.run(args + ['-o', 'json']))
        except (ValueError, UnicodeError):
            raise Blocked('native_api_response_invalid') from None

    def source_guard(self):
        require(not any(name.startswith('GIT_') for name in os.environ), 'git_environment_override')
        config = self.run(['git', '--no-optional-locks', '--no-replace-objects', 'config', '--no-includes', '--null', '--list']).decode().split('\0')
        for entry in config:
            key, _, value = entry.partition('\n')
            key = key.lower()
            require(not (key.startswith(('include.', 'includeif.', 'filter.')) or
                key in ('core.hookspath', 'core.sshcommand', 'diff.external', 'interactive.difffilter') or
                key.startswith('diff.') and key.endswith('.command') or
                key == 'core.fsmonitor' and value.lower() not in ('false', '0') or
                key == 'core.attributesfile' and value != '/dev/null'), 'unsafe_git_configuration')
        require(self.run(['git', '--no-optional-locks', '--no-replace-objects', 'rev-parse', 'HEAD']).decode().strip() == self.target_commit and
                self.run(['git', '--no-optional-locks', '--no-replace-objects', 'branch', '--show-current']).strip() == b'main', 'source_commit_drift')
        gitdir = Path(self.run(['git', '--no-optional-locks', '--no-replace-objects', 'rev-parse', '--absolute-git-dir']).decode().strip())
        require(gitdir == self.root / '.git' and gitdir.resolve() == gitdir, 'git_directory_binding_required')
        require(not self.run(['git', '--no-optional-locks', '--no-replace-objects', 'for-each-ref', '--format=%(refname)', 'refs/replace']).strip() and
                not (gitdir / 'info/grafts').exists() and not (gitdir / 'objects/info/alternates').exists(),
                'git_replacement_or_alternate_forbidden')
        require(not any((gitdir / name).exists() for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
                'rebase-merge', 'rebase-apply', 'index.lock')), 'git_operation_active')
        flags = self.run(['git', '--no-optional-locks', '--no-replace-objects', 'ls-files', '-v', '-z']).decode().split('\0')
        require(all(not x or x[0].isupper() and x[0] != 'S' for x in flags), 'hidden_git_index_flags')
        require(not self.run(['git', '--no-optional-locks', '--no-replace-objects', 'status', '--porcelain=v1', '--untracked-files=no']), 'tracked_source_dirty')
        self.run(['git', '--no-optional-locks', '--no-replace-objects', 'diff', '--cached', '--quiet', self.target_commit])

    def guards(self):
        resources = {}
        cluster = self.get('namespace', NS, 'kube-system')['metadata']['uid']
        nodes = [{'uid': n['metadata']['uid'], 'name': n['metadata']['name'],
                  'spec': n.get('spec'), 'addresses': n.get('status', {}).get('addresses')}
                 for n in self.get('nodes', NS)['items']]
        require(bool(cluster) and bool(nodes), 'cluster_node_identity_required')
        resources['cluster_node_sha256'] = digest({'cluster_uid': cluster,
            'nodes': sorted(nodes, key=lambda n: n['name'])})
        for namespace in (NS, 'monitoring'):
            for kind in ('secrets', 'persistentvolumeclaims', 'cronjobs', 'services', 'endpointslices', 'configmaps', 'deployments', 'pods'):
                items = self.get(kind, namespace)['items']
                projected = []
                for obj in items:
                    meta = obj['metadata']
                    value = {'name': meta['name'], 'uid': meta['uid']}
                    if kind == 'secrets':
                        value.update(resourceVersion=meta['resourceVersion'], type=obj.get('type'))
                    elif kind == 'endpointslices':
                        value.update({k: obj.get(k) for k in ('addressType', 'ports', 'endpoints')})
                    elif kind == 'configmaps':
                        value['data_sha256'] = digest({'data': obj.get('data'), 'binaryData': obj.get('binaryData')})
                    else:
                        value['spec'] = obj.get('spec')
                        if kind == 'persistentvolumeclaims':
                            require(obj.get('status', {}).get('phase') == 'Bound', 'protected_pvc_not_bound')
                    projected.append(value)
                resources[namespace + '/' + kind] = digest(sorted(projected, key=lambda p: p['name']))
        pods = self.get('pods', NS)['items']
        for app in ORDER:
            check_writer(self.get('deployment', NS, app), pods)
        state = self.get('configmap', NS, 'pvc-backup-sequence-state').get('data', {})
        require(state.get('status') == 'completed' and state.get('current') == 'none', 'backup_sequence_busy')
        for name in ('book-pvc-backup-state', 'youtube-pvc-backup-state', 'crawler-pvc-backup-state'):
            require(not self.get('configmap', NS, name).get('data', {}).get('lock_run_id'), 'backup_lock_busy')
        require(not self.get('configmap', 'monitoring', 'sre-telegram-backup-status').get('data', {}).get('lock_run_id'),
                'portal_backup_lock_busy')
        for job in self.get('jobs', NS)['items']:
            require(not job.get('status', {}).get('active', 0) and
                    any(c.get('type') in ('Complete', 'Failed') and c.get('status') == 'True'
                        for c in job.get('status', {}).get('conditions', [])), 'nonterminal_job_present')
        marker = self.run(['python3', str(self.root / 'scripts/runtime-service-state-reader.py'),
                          '/var/lib/personal-server/k3s-runtime-services.state', '--require-explicit']).decode().splitlines()
        require(set(marker) == {a + '=k3s' for a in ORDER if a != 'portal-web'}, 'runtime_writer_owner_required')
        compose = self.run(['docker', 'ps', '--format', '{{.Label "com.docker.compose.service"}}']).decode().splitlines()
        require(not set(compose) & (set(ORDER) | {'portal'}), 'duplicate_compose_writer')
        resources['runtime_owner_sha256'] = digest(sorted(marker))
        return digest(resources)

    def identity(self):
        require(self.collector_available(), 'trusted_native_collector_required')
        return self.collector.identity(self)

    def collect(self, app):
        require(app in ORDER and self.collector_available(), 'trusted_native_collector_required')
        return self.collector.collect(app, self)

    def verify(self, app, policy_uid=None):
        require(app in ORDER and self.collector_available(), 'trusted_native_collector_required')
        return self.collector.verify(app, self, policy_uid)

    def cleanup(self):
        return self.collector.cleanup(self) if self.collector_available() else True

    def policies(self):
        return self.get('networkpolicies', NS)['items']

    def current_policy(self, name):
        matches = [p for p in self.policies() if p['metadata']['name'] == name]
        require(len(matches) <= 1, 'policy_name_collision')
        return matches[0] if matches else None

    def create_policy(self, policy):
        try:
            meta, spec = policy.get('metadata', {}), policy.get('spec', {})
            app = next((a for a in ORDER if meta.get('name') == a + '-ingress-reviewed'), None)
            require(app is not None and meta.get('namespace') == NS and policy.get('kind') == 'NetworkPolicy' and
                    spec.get('podSelector') == {'matchLabels': {'app.kubernetes.io/name': app}} and
                    spec.get('policyTypes') == ['Ingress'] and 'egress' not in spec and
                    bool(re.fullmatch('[a-f0-9]{32}', meta.get('labels', {}).get(OWNER, ''))), 'invalid_policy_create')
            require(self.current_policy(meta['name']) is None, 'existing_policy_not_overwritten')
            require(callable(self.journal), 'durable_journal_required')
            self.journal({'event': 'native_create_intent', 'run_id': meta['labels'][OWNER],
                          'name': meta['name'], 'spec_sha256': digest(spec)})
        except Exception as exc:
            code = str(exc) if isinstance(exc, Blocked) else 'policy_create_not_attempted'
            raise CreateNotAttempted(code) from None
        return json.loads(self.run(KUBE + ['create', '-f', '-', '-o', 'json'], data=policy))

    def delete_policy(self, record, resource_version):
        obj = self.current_policy(record['name'])
        if obj is None:
            return
        check_owned(obj, record)
        require(obj['metadata'].get('resourceVersion') == resource_version, 'policy_resource_version_drift')
        uri = '/apis/networking.k8s.io/v1/namespaces/' + NS + '/networkpolicies/' + record['name']
        body = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions':
                {'uid': record['uid'], 'resourceVersion': resource_version}, 'propagationPolicy': 'Foreground'}
        require(callable(self.journal), 'durable_journal_required')
        self.journal({'event': 'native_delete_intent', 'policy': record, 'resource_version': resource_version})
        try:
            self.run(KUBE + ['delete', '--raw', uri, '-f', '-'], data=body)
        except Blocked:
            after = self.current_policy(record['name'])
            require(after is None or after['metadata'].get('uid') == record['uid'] and
                    after['metadata'].get('deletionTimestamp'), 'owned_delete_response_incomplete')
        deadline = time.monotonic() + 60
        while True:
            after = self.current_policy(record['name'])
            if after is None:
                return
            require(after['metadata'].get('uid') == record['uid'] and time.monotonic() < deadline,
                    'owned_policy_delete_incomplete')
            time.sleep(1)

    def external_health(self, app):
        require(app in HEALTH, 'unexpected_external_health_target')
        try:
            request = urllib.request.Request(HEALTH[app], headers={'User-Agent': 'fixed-ingress-guard/1'})
            with urllib.request.urlopen(request, timeout=8) as response:
                require(response.geturl() == HEALTH[app], 'external_health_redirect')
                return response.status
        except Blocked:
            raise
        except Exception:
            raise Blocked('external_health_failed') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--describe', action='store_true')
    args = parser.parse_args(argv)
    if not args.describe:
        print(json.dumps({'status': 'BLOCKED', 'reason': 'trusted_native_collector_required'}))
        return 2
    print(json.dumps({'production_authority': 'separately approved Python API invocation only',
        'trusted_native_collector_required': True, 'source_kind': 'fixed_pre_nat_ip',
        'order': list(ORDER), 'max_evidence_age_seconds': 300,
        'create_only': True, 'rollback': 'owned policy UID/spec/RV preconditions only',
        'caddy_only_identity_isolation': False, 'egress_unchanged': True}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
