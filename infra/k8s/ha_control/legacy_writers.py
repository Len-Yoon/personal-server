"""Bounded native WSL commands for the legacy cutover's Linux writer stages.

The caller must hold the host/deployment exclusion for the full transaction,
bind a private baseline to its plan and positively settle any uncertain command
before asking for rollback. This module does not fence Windows recovery actors.
"""
from copy import deepcopy
import json
import os
import re
import subprocess
import time

from ha_runtime.config import SERVICES

from .controller import TransitionBlocked


KUBE = ('/usr/bin/sudo', '-n', '/usr/local/bin/k3s', 'kubectl')
DOCKER = ('/usr/bin/docker', '--host', 'unix:///var/run/docker.sock')
K3S_SERVICES = frozenset(('portal-web', 'book-memo', 'youtube-memo', 'crawler-worker'))
COMPOSE_ORDER = ('homeops-executor', 'car-care-worker', 'system-agent',
                 'portal-web', 'book-memo', 'youtube-memo', 'crawler-worker')
NAME = re.compile(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z')
UID = re.compile(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\Z')
CONTAINER = re.compile(r'[a-f0-9]{64}\Z')
IMAGE = re.compile(r'sha256:[a-f0-9]{64}\Z')


def _need(condition, code='legacy_writer_scope_rejected'):
    if not condition:
        raise TransitionBlocked(code)


def _match(pattern, value):
    return type(value) is str and pattern.fullmatch(value) is not None


def _run(args, timeout=10):
    environment = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C', 'HOME': os.path.expanduser('~')}
    try:
        return subprocess.run(args, capture_output=True, check=True, timeout=timeout, env=environment).stdout
    except (OSError, subprocess.SubprocessError):
        # A timeout or failed transport does not establish whether a mutation took effect.
        raise TransitionBlocked('legacy_writer_command_unconfirmed') from None


class LinuxWriters:
    """Operate only on exact bound identities in an already-reviewed writer graph."""

    def __init__(self, baseline, runner=_run):
        self.runner = runner
        self._baseline = deepcopy(baseline)
        self._validate(self._baseline)

    @staticmethod
    def _validate(baseline):
        _need(type(baseline) is dict and set(baseline) == {'deployments', 'schedules', 'containers'})
        deployments, schedules, containers = (baseline[k] for k in ('deployments', 'schedules', 'containers'))
        _need(type(deployments) is dict and set(deployments) == K3S_SERVICES
              and type(containers) is dict and set(containers) == set(SERVICES)
              and type(schedules) is list and len(schedules) == 4)
        for row in (*deployments.values(), *schedules):
            _need(type(row) is dict and all(_match(NAME, row.get(k)) for k in ('namespace', 'name'))
                  and _match(UID, row.get('uid')))
        _need(len({x['uid'] for x in (*deployments.values(), *schedules)}) == len(deployments) + len(schedules))
        for service, row in deployments.items():
            _need(row['namespace'] == 'personal-server' and row['name'] == service
                  and set(row) == {'namespace', 'name', 'uid', 'replicas'}
                  and type(row['replicas']) is int and row['replicas'] > 0)
        for row in schedules:
            _need(row['namespace'] == 'personal-server'
                  and set(row) == {'namespace', 'name', 'uid', 'suspend'} and type(row['suspend']) is bool)
        for service, row in containers.items():
            _need(type(row) is dict and set(row) == {'id', 'image', 'running', 'restart'}
                  and _match(CONTAINER, row['id']) and _match(IMAGE, row['image'])
                  and type(row['running']) is bool
                  and (service == 'system-agent' or row['running'] is (service not in K3S_SERVICES))
                  and row['restart'] == 'unless-stopped')
        _need(len({row['id'] for row in containers.values()}) == len(containers))

    def _json(self, args):
        try:
            return json.loads(self.runner(args))
        except (ValueError, TypeError):
            raise TransitionBlocked('legacy_writer_response_rejected') from None

    def _resource(self, kind, row):
        resource = self._json((*KUBE, '-n', row['namespace'], 'get', kind, row['name'], '-o', 'json'))
        meta = resource['metadata']
        _need(meta['uid'] == row['uid'] and meta['name'] == row['name'] and meta['namespace'] == row['namespace']
              and not meta.get('deletionTimestamp'), 'legacy_writer_identity_changed')
        return resource

    def _patch(self, kind, row, field, current, desired, resource):
        if type(current) is not type(desired):
            raise TransitionBlocked('legacy_writer_response_rejected')
        if current == desired:
            return
        version = resource['metadata']['resourceVersion']
        _need(type(version) is str and re.fullmatch(r'[0-9]+', version) is not None)
        path = '/spec/' + field
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': row['uid']},
                 {'op': 'test', 'path': '/metadata/resourceVersion', 'value': version},
                 {'op': 'add', 'path': path, 'value': desired}]
        response = self._json((*KUBE, '-n', row['namespace'], 'patch', kind, row['name'],
                               '--type=json', '-o', 'json', '-p', json.dumps(patch, separators=(',', ':'))))
        _need(response['metadata']['uid'] == row['uid'] and response['spec'][field] == desired,
              'legacy_writer_command_unconfirmed')

    def _container(self, service):
        row = self._baseline['containers'][service]
        # Look up the Compose service as well as its ID to catch replacements.
        ids = self.runner((*DOCKER, 'ps', '-aq', '--no-trunc', '--filter',
                           'label=com.docker.compose.service=' + service)).decode('ascii').split()
        _need(ids == [row['id']], 'legacy_writer_identity_changed')
        items = self._json((*DOCKER, 'inspect', row['id']))
        _need(type(items) is list and len(items) == 1, 'legacy_writer_response_rejected')
        item = items[0]
        _need(item['Id'] == row['id'] and item['Image'] == row['image']
              and item['Config']['Labels']['com.docker.compose.service'] == service,
              'legacy_writer_identity_changed')
        policy = item['HostConfig']['RestartPolicy']
        state = item['State']
        running = state['Running']
        _need(not any(state.get(key) for key in ('Paused', 'Restarting', 'Dead', 'OOMKilled'))
              and state['Status'] in (('running',) if running else ('created', 'exited')),
              'legacy_writer_state_drift')
        _need(type(running) is bool and policy.get('Name') in ('no', row['restart'])
              and policy.get('MaximumRetryCount') == 0, 'legacy_writer_state_drift')
        return running, policy['Name']

    def _deployment(self, row):
        resource = self._resource('deployment', row)
        replicas = resource['spec'].get('replicas', 1)
        _need(type(replicas) is int and replicas in (0, row['replicas']), 'legacy_writer_state_drift')
        return resource, replicas

    def _schedule(self, row):
        resource = self._resource('cronjob', row)
        suspend = resource['spec'].get('suspend', False)
        _need(type(suspend) is bool and (suspend or suspend == row['suspend'])
              and not resource.get('status', {}).get('active'), 'legacy_writer_schedule_active_or_drifted')
        return resource, suspend

    def _no_autoscaler(self):
        targets = {(row['namespace'], row['name']) for row in self._baseline['deployments'].values()}
        response = self._json((*KUBE, 'get', 'hpa', '-A', '-o', 'json'))
        for item in response['items']:
            ref = item['spec']['scaleTargetRef']
            _need(ref['kind'] != 'Deployment' or (item['metadata']['namespace'], ref['name']) not in targets,
                  'legacy_writer_autoscaler_present')

    def _objects(self, *kinds):
        response = self._json((*KUBE, '-n', 'personal-server', 'get', ','.join(kinds), '-o', 'json'))
        return response['items']

    def _assert_schedule_graph(self):
        deployments = self._objects('deployment')
        ids = {row['uid'] for row in self._baseline['deployments'].values()}
        claims = set()
        for deployment in deployments:
            if deployment['metadata']['uid'] in ids:
                claims.update(self._claims(deployment['spec']['template']['spec']))
        _need(bool(claims), 'legacy_writer_storage_unconfirmed')
        schedules = {
            row['metadata']['uid'] for row in self._objects('cronjob')
            if self._claims(row['spec']['jobTemplate']['spec']['template']['spec']) & claims
        }
        _need(schedules == {row['uid'] for row in self._baseline['schedules']},
              'legacy_writer_schedule_scope_changed')
        return claims

    @staticmethod
    def _claims(pod_spec):
        return {volume['persistentVolumeClaim']['claimName']
                for volume in pod_spec.get('volumes', ()) if 'persistentVolumeClaim' in volume}

    @staticmethod
    def _owned(item, kind, owners):
        return any(ref.get('controller') is True and ref['kind'] == kind and ref['uid'] in owners
                   for ref in item['metadata'].get('ownerReferences', ()))

    def _writers_active(self, claims, *, include_deployments):
        schedules = {row['uid'] for row in self._baseline['schedules']}
        deployments = {row['uid'] for row in self._baseline['deployments'].values()}
        objects = self._objects('jobs', 'replicasets', 'pods')
        jobs = set()
        replicasets = set()
        for item in objects:
            if item['kind'] == 'Job' and (self._owned(item, 'CronJob', schedules)
                                          or self._claims(item['spec']['template']['spec']) & claims):
                jobs.add(item['metadata']['uid'])
                if not any(cond['type'] in ('Complete', 'Failed') and cond['status'] == 'True'
                           for cond in item.get('status', {}).get('conditions', ())):
                    return True
            if (include_deployments and item['kind'] == 'ReplicaSet'
                    and self._owned(item, 'Deployment', deployments)):
                replicasets.add(item['metadata']['uid'])
                if item['spec'].get('replicas', 1) or item.get('status', {}).get('replicas', 0):
                    return True
        return any(item['kind'] == 'Pod' and item.get('status', {}).get('phase') not in ('Succeeded', 'Failed')
                   and (self._owned(item, 'Job', jobs) or self._owned(item, 'ReplicaSet', replicasets)
                        or (include_deployments and self._claims(item['spec']) & claims))
                   for item in objects)

    @staticmethod
    def _await(condition, reason):
        deadline = time.monotonic() + 45
        while not condition():
            _need(time.monotonic() < deadline, reason)
            time.sleep(1)

    def assert_baseline(self):
        """Read the actual originals before permitting any mutation."""
        self._no_autoscaler()
        claims = self._assert_schedule_graph()
        for row in self._baseline['deployments'].values():
            resource, replicas = self._deployment(row)
            _need(replicas == row['replicas'] and resource.get('status', {}).get('readyReplicas') == replicas,
                  'legacy_writer_original_not_ready')
        for row in self._baseline['schedules']:
            _, suspend = self._schedule(row)
            _need(suspend == row['suspend'], 'legacy_writer_state_drift')
        _need(not self._writers_active(claims, include_deployments=False), 'legacy_writer_schedule_active_or_drifted')
        for service, row in self._baseline['containers'].items():
            _need(self._container(service) == (row['running'], row['restart']), 'legacy_writer_state_drift')

    def block_reactivation(self):
        """Suppress known Linux restart policies while leaving app processes running."""
        self.assert_baseline()
        for row in self._baseline['schedules']:
            resource, suspend = self._schedule(row)
            self._patch('cronjob', row, 'suspend', suspend, True, resource)
        for service in COMPOSE_ORDER:
            running, restart = self._container(service)
            row = self._baseline['containers'][service]
            _need((running, restart) == (row['running'], row['restart']), 'legacy_writer_state_drift')
            self.runner((*DOCKER, 'update', '--restart=no', row['id']))
            _need(self._container(service) == (running, 'no'), 'legacy_writer_command_unconfirmed')

    def fence(self):
        """Close exact Compose writers and scale only the four bound deployments."""
        self._no_autoscaler()
        for row in self._baseline['schedules']:
            _need(self._schedule(row)[1], 'legacy_writer_reactivation_not_blocked')
        claims = self._assert_schedule_graph()
        _need(not self._writers_active(claims, include_deployments=False), 'legacy_writer_schedule_active_or_drifted')
        for row in self._baseline['deployments'].values():
            self._deployment(row)
        for service in COMPOSE_ORDER:
            _need(self._container(service)[1] == 'no', 'legacy_writer_reactivation_not_blocked')
        for service in COMPOSE_ORDER:
            running, restart = self._container(service)
            row = self._baseline['containers'][service]
            _need(restart == 'no' and (not running or row['running']), 'legacy_writer_reactivation_not_blocked')
            if running:
                self.runner((*DOCKER, 'stop', '-t', '10', row['id']), timeout=20)
            _need(self._container(service) == (False, 'no'), 'legacy_writer_command_unconfirmed')
        for row in self._baseline['deployments'].values():
            resource, replicas = self._deployment(row)
            self._patch('deployment', row, 'replicas', replicas, 0, resource)

        self.assert_closed()

    def assert_closed(self):
        """Wait for old workload pods to exit, including terminating pods."""
        self._no_autoscaler()
        claims = self._assert_schedule_graph()
        for row in self._baseline['deployments'].values():
            _need(self._deployment(row)[1] == 0, 'legacy_writer_deployment_reopened')
        for row in self._baseline['schedules']:
            _need(self._schedule(row)[1], 'legacy_writer_reactivation_not_blocked')
        self._await(lambda: self._deployments_closed()
                    and not self._writers_active(claims, include_deployments=True), 'legacy_writer_pods_still_active')
        for service in COMPOSE_ORDER:
            _need(self._container(service) == (False, 'no'), 'legacy_writer_container_reopened')

    def restore(self):
        """Restore only known original/closed values after external rollback proof."""
        self._no_autoscaler()
        claims = self._assert_schedule_graph()
        for row in self._baseline['deployments'].values():
            self._deployment(row)
        for row in self._baseline['schedules']:
            self._schedule(row)
        _need(not self._writers_active(claims, include_deployments=False), 'legacy_writer_schedule_active_or_drifted')
        for service, row in self._baseline['containers'].items():
            _need(not self._container(service)[0] or row['running'], 'legacy_writer_state_drift')
        for row in self._baseline['deployments'].values():
            resource, replicas = self._deployment(row)
            self._patch('deployment', row, 'replicas', replicas, row['replicas'], resource)
        for service in reversed(COMPOSE_ORDER):
            running, restart = self._container(service)
            row = self._baseline['containers'][service]
            _need(not running or row['running'], 'legacy_writer_state_drift')
            if restart != row['restart']:
                self.runner((*DOCKER, 'update', '--restart='+row['restart'], row['id']))
            if row['running'] and not running:
                self.runner((*DOCKER, 'start', row['id']), timeout=20)
            _need(self._container(service) == (row['running'], row['restart']), 'legacy_writer_command_unconfirmed')
        self._await(self._deployments_ready, 'legacy_writer_original_not_ready')
        for row in self._baseline['schedules']:
            resource, suspend = self._schedule(row)
            self._patch('cronjob', row, 'suspend', suspend, row['suspend'], resource)

    def _deployments_closed(self):
        for row in self._baseline['deployments'].values():
            resource, replicas = self._deployment(row)
            status = resource.get('status', {})
            if (replicas != 0 or status.get('replicas', 0) != 0
                    or status.get('observedGeneration') != resource['metadata']['generation']):
                return False
        return True

    def _deployments_ready(self):
        for row in self._baseline['deployments'].values():
            resource, replicas = self._deployment(row)
            status = resource.get('status', {})
            if (replicas != row['replicas'] or status.get('readyReplicas') != replicas
                    or status.get('observedGeneration') != resource['metadata']['generation']):
                return False
        return True


def inspect_live_baseline(runner=_run):
    """Discover and read back the fixed production Linux scope without mutations."""
    def objects(kind):
        return json.loads(runner((*KUBE, '-n', 'personal-server', 'get', kind, '-o', 'json')))['items']

    def identity(item):
        return {key: item['metadata'][key] for key in ('namespace', 'name', 'uid')}

    deployments = {}
    claims = set()
    for item in objects('deployments'):
        service = item['metadata']['name']
        if service in K3S_SERVICES:
            deployments[service] = {**identity(item), 'replicas': item['spec'].get('replicas', 1)}
            claims.update(LinuxWriters._claims(item['spec']['template']['spec']))
    schedules = [
        {**identity(item), 'suspend': item['spec'].get('suspend', False)}
        for item in objects('cronjobs')
        if LinuxWriters._claims(item['spec']['jobTemplate']['spec']['template']['spec']) & claims
    ]
    containers = {}
    for service in COMPOSE_ORDER:
        ids = runner((*DOCKER, 'ps', '-aq', '--no-trunc', '--filter',
                      'label=com.docker.compose.service=' + service)).decode('ascii').split()
        _need(len(ids) == 1 and _match(CONTAINER, ids[0]), 'legacy_writer_identity_changed')
        item, = json.loads(runner((*DOCKER, 'inspect', ids[0])))
        containers[service] = {'id': ids[0], 'image': item['Image'],
                               'running': item['State']['Running'],
                               'restart': item['HostConfig']['RestartPolicy']['Name']}
    baseline = {'deployments': deployments, 'schedules': schedules, 'containers': containers}
    LinuxWriters(baseline, runner).assert_baseline()
    return baseline
