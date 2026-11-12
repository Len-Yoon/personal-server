"""Observe and replace the one production Compose Caddy container."""

import copy
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

from .caddy_cutover import MOUNT, UPSTREAMS, render
from .controller import TransitionBlocked

CONFIG = '/etc/caddy/Caddyfile'
COMPOSE = ('docker-compose.yml', 'docker-compose.n100.yml', 'docker-compose.portal-bridge.yml')
DOCKER_CONFIG = Path('/etc/docker')
DOCKER_SOCKET = Path('/var/run/docker.sock')
DOCKER = ('/usr/bin/docker', '--config', str(DOCKER_CONFIG), '--host', f'unix://{DOCKER_SOCKET}')
PLUGIN_DIRS = ('/usr/local/lib/docker/cli-plugins', '/usr/local/libexec/docker/cli-plugins',
               '/usr/lib/docker/cli-plugins', '/usr/libexec/docker/cli-plugins')
TLS_FILES = ('server-ca.crt', 'client.crt', 'client.key')
MTLS_VOLUME = 'n100-ha-caddy-mtls'
ID = re.compile(r'[a-f0-9]{64}\Z')
IMAGE = re.compile(r'sha256:[a-f0-9]{64}\Z')
LABEL = 'com.docker.compose.'


def _need(condition, reason):
    if not condition:
        raise TransitionBlocked(reason)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _pin(value):
    return _sha(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def _run(args, *, timeout=20, cwd=None, input_data=None, compose_env=None):
    _assert_docker_paths(args[len(DOCKER)] == 'compose')
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'HOME': str(Path.home()), 'COMPOSE_REMOVE_ORPHANS': 'false'}
    env.update(compose_env or {})
    return subprocess.run(args, cwd=cwd, env=env, input=input_data, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=timeout).stdout


def _system_path(path):
    # Resolve distribution symlinks only through directories owned by root.
    resolved = path.resolve(strict=True)
    for parent in {*path.parents, *resolved.parents, resolved}:
        info = parent.stat()
        _need(info.st_uid == 0 and not info.st_mode & 0o022, 'caddy_docker_path_untrusted')
    return resolved.stat()


def _assert_docker_paths(compose):
    binary = _system_path(Path(DOCKER[0]))
    _need(stat.S_ISREG(binary.st_mode) and bool(binary.st_mode & stat.S_IXUSR), 'caddy_docker_path_untrusted')
    _need(stat.S_ISDIR(_system_path(DOCKER_CONFIG).st_mode), 'caddy_docker_path_untrusted')
    config = DOCKER_CONFIG / 'config.json'
    if os.path.lexists(config):
        _need(stat.S_ISREG(_system_path(config).st_mode), 'caddy_docker_path_untrusted')
        _need(not json.loads(_read(config)).get('cliPluginsExtraDirs'), 'caddy_docker_path_untrusted')
    socket = DOCKER_SOCKET.resolve(strict=True)
    _need(stat.S_ISDIR(_system_path(DOCKER_SOCKET.parent).st_mode), 'caddy_docker_path_untrusted')
    info = socket.stat()
    _need(stat.S_ISSOCK(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o002,
          'caddy_docker_path_untrusted')
    if compose:
        candidates = [path / 'docker-compose' for path in
                      (DOCKER_CONFIG / 'cli-plugins', *map(Path, PLUGIN_DIRS))]
        available = [_system_path(path) for path in candidates if os.path.lexists(path)]
        _need(available and all(stat.S_ISREG(info.st_mode) and info.st_mode & stat.S_IXUSR
                                for info in available), 'caddy_docker_path_untrusted')


def _safe_path(path):
    path = Path(path)
    _need(path.is_absolute() and '..' not in path.parts and path.resolve() == path
          and not any(char in str(path) for char in ',:$\r\n'), 'caddy_path_untrusted')
    return path


def _private_dir(path):
    path = _safe_path(path)
    for directory in (path, *path.parents):
        info = directory.lstat()
        _need(stat.S_ISDIR(info.st_mode) and info.st_uid in {0, os.geteuid()}
              and not info.st_mode & 0o022, 'caddy_directory_untrusted')
    _need(not path.stat().st_mode & 0o077, 'caddy_directory_untrusted')
    return path


def _read(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as source:
        info = os.fstat(source.fileno())
        _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid in {0, os.geteuid(), 10001}
              and not info.st_mode & 0o022 and 0 < info.st_size <= 1048576, 'caddy_file_untrusted')
        raw = source.read(1048577)
        _need(len(raw) == info.st_size and os.fstat(source.fileno()).st_mtime_ns == info.st_mtime_ns,
              'caddy_file_changed')
        return raw


def _write(path, data, mode):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode), 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_dir(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _tls_files(raw):
    rows = raw.decode('ascii').splitlines()
    files = [line.split() for line in rows]
    _need(len(files) == len(TLS_FILES) and all(len(row) == 2 and ID.fullmatch(row[0]) for row in files)
          and {row[1] for row in files} == {MOUNT + '/' + name for name in TLS_FILES}, 'caddy_mtls_files_unconfirmed')
    return {Path(path).name: digest for digest, path in files}


def _runtime_contract(container, added=()):
    config = copy.deepcopy(container['Config'])
    if config.get('Hostname') == container['Id'][:12]:
        config['Hostname'] = '<docker-generated>'
    config.pop('Image')
    config['Env'] = sorted(config['Env'])
    for key in ('config-hash', 'depends_on', 'replace', 'version', 'project.config_files'):
        config['Labels'].pop(LABEL + key, None)
    host = copy.deepcopy(container['HostConfig'])
    for key in ('Binds', 'Mounts'):
        remaining = [item for item in host.get(key) or []
                     if (item.split(':')[1] if key == 'Binds' else item['Target']) not in added]
        host[key] = sorted(remaining, key=_pin)
    networks = {name: {key: value.get(key) for key in ('NetworkID', 'IPAMConfig', 'DriverOpts', 'GwPriority')}
                for name, value in container['NetworkSettings']['Networks'].items()}
    return _pin({'config': config, 'host': host, 'networks': networks,
                 'ports': container['NetworkSettings']['Ports']})


@dataclass(frozen=True)
class PreparedCaddy:
    baseline: dict
    override: Path
    candidate: Path
    overlay_sha256: str
    candidate_sha256: str
    mtls_volume_sha256: str
    mtls_files_sha256: str


class CaddyRuntime:
    """Use a held caller guard and retain any possibly applied candidate on disk."""

    def __init__(self, root, project, *, run=_run):
        self.root = _safe_path(root)
        _need(type(project) is str and re.fullmatch(r'[a-z][a-z0-9_-]{0,62}', project),
              'caddy_project_unconfirmed')
        self.project = project
        self.run = run
        self.attempted = False
        self.prepared = None

    def _layout(self, item, *, isolated=False):
        labels = item['Config']['Labels']
        actual = labels[LABEL + 'project.config_files'].split(',')
        expected = [str(self.root / name) for name in COMPOSE]
        allowed = (['-'], [str(self.root / '-')]) if isolated else (expected[:2], expected)
        _need(actual in allowed, 'caddy_compose_layout_unconfirmed')
        return actual

    def _docker(self, *args, timeout=20, input_data=None, compose_env=None):
        try:
            options = {'input_data': input_data} if input_data is not None else {}
            if compose_env is not None:
                options['compose_env'] = compose_env
            return self.run((*DOCKER, *args), timeout=timeout, cwd=self.root, **options)
        except (OSError, subprocess.SubprocessError):
            raise TransitionBlocked('caddy_docker_command_unconfirmed') from None

    def _compose(self, files, *args, override=None, timeout=20, input_data=None, compose_env=None):
        env_file = '/dev/null' if input_data is not None else str(self.root / '.env')
        command = ['compose', '--project-name', self.project, '--project-directory', str(self.root),
                   '--env-file', env_file]
        for path in (*files, *((override,) if override else ())):
            command.extend(('-f', str(path)))
        return self._docker(*command, *args, timeout=timeout, input_data=input_data, compose_env=compose_env)

    def _config(self, files, upstreams, override=None):
        result = json.loads(self._compose(files, 'config', '--format', 'json', override=override, compose_env=upstreams))
        _need(result.get('name') == self.project and 'caddy' in result.get('services', {}),
              'caddy_compose_unconfirmed')
        return result

    def _service_hash(self, files, upstreams):
        row = self._compose(files, 'config', '--hash', 'caddy', compose_env=upstreams).decode('ascii').split()
        _need(len(row) == 2 and row[0] == 'caddy' and ID.fullmatch(row[1]), 'caddy_compose_hash_unconfirmed')
        return row[1]

    def _container(self, *, isolated=False):
        ids = self._docker('ps', '-aq', '--no-trunc', '--filter', f'label={LABEL}project={self.project}',
                           '--filter', f'label={LABEL}service=caddy').decode('ascii').split()
        _need(len(ids) == 1 and ID.fullmatch(ids[0]), 'caddy_identity_unconfirmed')
        item, = json.loads(self._docker('inspect', ids[0]))
        labels = item['Config']['Labels']
        self._layout(item, isolated=isolated)
        _need(item['Id'] == ids[0]
              and labels.get(LABEL + 'project') == self.project and labels.get(LABEL + 'service') == 'caddy'
              and labels.get(LABEL + 'oneoff') == 'False' and labels.get(LABEL + 'project.working_dir') == str(self.root)
              and labels.get(LABEL + 'container-number') == '1'
              and item['State']['Running'] is True and item['State']['Status'] == 'running'
              and not any(item['State'].get(flag) for flag in ('Paused', 'Restarting', 'Dead', 'OOMKilled'))
              and IMAGE.fullmatch(item['Image']), 'caddy_identity_unconfirmed')
        return item

    @staticmethod
    def _upstreams(item):
        values = {}
        for entry in item['Config']['Env']:
            key, _, value = entry.partition('=')
            if key in UPSTREAMS:
                _need(key not in values and re.fullmatch(r'[A-Za-z0-9.-]{1,253}:[0-9]{1,5}', value)
                      and 0 < int(value.rpartition(':')[2]) <= 65535, 'caddy_upstream_environment_unconfirmed')
                values[key] = value
        _need(set(values) == set(UPSTREAMS), 'caddy_upstream_environment_unconfirmed')
        return values

    def _image(self, image):
        item, = json.loads(self._docker('image', 'inspect', image))
        _need(item['Id'] == image, 'caddy_image_changed')

    def _network(self, config, item):
        service = config['services']['caddy']
        deployment = service.get('deploy') or {}
        for scale in (service.get('scale', 1), deployment.get('replicas', 1)):
            _need(type(scale) is int and scale == 1, 'caddy_replica_scope_changed')
        _need(deployment.get('mode', 'replicated') == 'replicated' and not service.get('network_mode'),
              'caddy_replica_scope_changed')
        key, = service['networks']
        desired = config['networks'][key]
        name = desired['name']
        attached = item['NetworkSettings']['Networks']
        _need(not desired.get('external') and set(attached) == {name}, 'caddy_network_scope_changed')
        network_id = attached[name]['NetworkID']
        _need(ID.fullmatch(network_id) and item['HostConfig']['NetworkMode'] in (name, network_id),
              'caddy_network_scope_changed')
        matching_ids = self._docker('network', 'ls', '--no-trunc', '--filter', 'name=^' + re.escape(name) + '$',
                                    '--format', '{{.ID}}').decode('ascii').split()
        _need(matching_ids == [network_id], 'caddy_network_name_ambiguous')
        network, = json.loads(self._docker('network', 'inspect', network_id))
        _need(network['Id'] == network_id and network['Name'] == name and network['Scope'] == 'local'
              and network['Driver'] == desired.get('driver', 'bridge') == 'bridge'
              and (network.get('Options') or {}) == (desired.get('driver_opts') or {})
              and not network.get('Ingress') and not network.get('ConfigOnly')
              and not (network.get('ConfigFrom') or {}).get('Network'), 'caddy_network_scope_changed')
        for actual, source, default in (('Internal', 'internal', False), ('Attachable', 'attachable', False),
                                        ('EnableIPv4', 'enable_ipv4', True), ('EnableIPv6', 'enable_ipv6', False)):
            _need(network.get(actual, default) is desired.get(source, default), 'caddy_network_scope_changed')
        labels = dict(network['Labels'])
        _need(labels.pop(LABEL + 'project', None) == self.project and labels.pop(LABEL + 'network', None) == key,
              'caddy_network_scope_changed')
        for generated in ('version', 'config-hash'):
            labels.pop(LABEL + generated, None)
        _need(labels == (desired.get('labels') or {}), 'caddy_network_scope_changed')
        ipam = network['IPAM']
        desired_ipam = desired.get('ipam') or {}
        _need(ipam['Driver'] == desired_ipam.get('driver', 'default') and
              (ipam.get('Options') or {}) == (desired_ipam.get('options') or {}) and not desired_ipam.get('config'),
              'caddy_network_scope_changed')
        # The existing network uses engine-assigned IPAM. Pin its assigned
        # subnets and remaining immutable metadata across the replacement.
        return _pin({key: value for key, value in network.items() if key != 'Containers'})

    def _mtls_volume(self):
        item, = json.loads(self._docker('volume', 'inspect', MTLS_VOLUME))
        _need(item['Name'] == MTLS_VOLUME and item['Driver'] == 'local' and item['Scope'] == 'local'
              and not item.get('Options') and str(item['Mountpoint']).startswith('/'), 'caddy_mtls_volume_unconfirmed')
        return _pin(item), item['Mountpoint']

    def _mounts(self, item, added=()):
        mounts = {row['Destination']: row for row in item['Mounts']}
        _need(len(mounts) == len(item['Mounts']) and set(mounts) == {'/data', '/config', *added},
              'caddy_mount_scope_changed')
        volumes = {}
        for path in ('/data', '/config'):
            mount = mounts[path]
            _need(mount['Type'] == 'volume' and mount['RW'] is True and mount['Driver'] == 'local'
                  and isinstance(mount['Name'], str), 'caddy_persistent_volume_unconfirmed')
            volume, = json.loads(self._docker('volume', 'inspect', mount['Name']))
            labels = volume.get('Labels') or {}
            _need(volume['Name'] == mount['Name'] and volume['Mountpoint'] == mount['Source']
                  and volume['Driver'] == 'local' and labels.get(LABEL + 'project') == self.project
                  and labels.get(LABEL + 'volume') == 'caddy_' + path[1:], 'caddy_persistent_volume_unconfirmed')
            volumes[path] = _pin({'mount': mount, 'volume': volume})
        return volumes, mounts

    def _live_config(self, container, *, starting=False):
        raw = self._docker('exec', container, '/bin/cat', CONFIG)
        deadline = time.monotonic() + (5 if starting else 0)
        while True:
            try:
                live = json.loads(self._docker('exec', container, '/usr/bin/wget', '-q', '-T', '1', '-O', '-',
                                               'http://127.0.0.1:2019/config/', timeout=2))
                break
            except TransitionBlocked:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        adapted = json.loads(self._docker('exec', container, '/usr/bin/caddy', 'adapt', '--adapter', 'caddyfile',
                                          '--config', CONFIG))
        _need(type(live) is dict and bool(live.get('apps', {}).get('http')) and live == adapted,
              'caddy_live_config_unconfirmed')
        return raw, _pin(live)

    def _snapshot(self):
        item = self._container()
        files = self._layout(item)
        upstreams = self._upstreams(item)
        config = self._config(files, upstreams)
        service_hash = self._service_hash(files, upstreams)
        _need(item['Config']['Labels'].get(LABEL + 'config-hash') == service_hash, 'caddy_live_compose_drift')
        network = self._network(config, item)
        self._image(item['Image'])
        volumes, mounts = self._mounts(item)
        ports = item['NetworkSettings']['Ports']
        _need({'80/tcp', '443/tcp'} <= set(ports) <= {'80/tcp', '443/tcp', '443/udp', '2019/tcp'},
              'caddy_port_scope_changed')
        for port in ('80', '443'):
            published = ports[port + '/tcp']
            _need(published and all(row['HostPort'] == port and row['HostIp'] in ('0.0.0.0', '::')
                                    for row in published), 'caddy_port_scope_changed')
        _need(not ports.get('443/udp') and not ports.get('2019/tcp'), 'caddy_port_scope_changed')
        raw, live = self._live_config(item['Id'])
        return {'id': item['Id'], 'image': item['Image'], 'compose_files': files, 'compose_sha256': _pin(config),
                'service_sha256': service_hash,
                'network_sha256': network,
                'container_config_sha256': _pin(item['Config']), 'started_at_sha256': _pin(item['State']['StartedAt']),
                'runtime_sha256': _runtime_contract(item), 'source_sha256': _sha(raw),
                'live_sha256': live, 'volumes': volumes,
                'volume_names': {path: mount['Name'] for path, mount in mounts.items()}}, raw, upstreams

    def inspect(self):
        try:
            return self._snapshot()[0]
        except (OSError, KeyError, ValueError, TypeError, AttributeError):
            raise TransitionBlocked('caddy_baseline_unconfirmed') from None

    def assert_original(self, baseline):
        _need(self.inspect() == baseline, 'caddy_baseline_changed')

    @staticmethod
    def _bind(path, target):
        return {'type': 'bind', 'source': str(path), 'target': target, 'read_only': True,
                'bind': {'create_host_path': False}}

    @staticmethod
    def _mtls_mount():
        return {'type': 'volume', 'source': MTLS_VOLUME, 'target': MOUNT, 'read_only': True,
                'volume': {'nocopy': True}}

    def _effective(self, prepared, upstreams):
        files = prepared.baseline['compose_files']
        base = self._config(files, upstreams)
        _need(_pin(base) == prepared.baseline['compose_sha256'], 'caddy_baseline_changed')
        expected = copy.deepcopy(base)
        service = expected['services']['caddy']
        service['image'] = prepared.baseline['image']
        service['volumes'] += [self._bind(prepared.candidate, CONFIG), self._mtls_mount()]
        _need(MTLS_VOLUME not in expected['volumes'], 'caddy_mtls_volume_alias_conflict')
        expected['volumes'][MTLS_VOLUME] = {'external': True, 'name': MTLS_VOLUME}
        actual = self._config(files, upstreams, prepared.override)
        for config in (expected, actual):
            volumes = config['services']['caddy']['volumes']
            volumes.sort(key=lambda row: row['target'])
        _need(actual == expected, 'caddy_compose_override_scope_changed')
        return actual

    def _prepared(self, prepared, upstreams):
        _need(_sha(_read(prepared.override)) == prepared.overlay_sha256
              and _sha(_read(prepared.candidate)) == prepared.candidate_sha256
              and self._mtls_volume()[0] == prepared.mtls_volume_sha256, 'caddy_prepared_files_changed')
        return self._effective(prepared, upstreams)

    def _isolated(self, effective, prepared):
        service = copy.deepcopy(effective['services']['caddy'])
        for key in ('build', 'depends_on'):
            service.pop(key, None)
        network_key, = service['networks']
        network = effective['networks'][network_key]
        names = prepared.baseline['volume_names'] | {MOUNT: MTLS_VOLUME}
        volumes = {}
        for mount in service['volumes']:
            if mount['type'] == 'volume':
                name = effective['volumes'][mount['source']]['name']
                _need(name == names[mount['target']], 'caddy_existing_volume_name_changed')
                volumes[mount['source']] = {'name': name, 'external': True}
        _need(len(volumes) == 3, 'caddy_existing_volume_name_changed')
        model = {'name': self.project, 'services': {'caddy': service}, 'volumes': volumes,
                 'networks': {network_key: {'name': network['name'], 'external': True}}}
        # Compose config emits escaped literal dollars. Keep the resolved
        # service values in memory and pass the same representation via stdin.
        raw = json.dumps(model).encode()
        rendered = json.loads(self._compose(('-',), 'config', '--format', 'json', input_data=raw))
        rendered['services']['caddy']['volumes'].sort(key=lambda row: row['target'])
        if rendered['networks'][network_key].get('ipam') == {}:
            rendered['networks'][network_key].pop('ipam')
        _need(rendered == model, 'caddy_isolated_compose_scope_changed')
        return raw

    def _check_prepared_credentials(self, prepared):
        _need(self._mtls_volume()[0] == prepared.mtls_volume_sha256, 'caddy_mtls_volume_changed')
        raw = self._docker('run', '--rm', '--pull=never', '--network=none', '--read-only',
                           '--tmpfs', '/data', '--tmpfs', '/config',
                           '--mount', f'type=volume,src={MTLS_VOLUME},dst={MOUNT},readonly,volume-nocopy',
                           '--entrypoint', '/bin/busybox', prepared.baseline['image'], 'sha256sum',
                           *(MOUNT + '/' + name for name in TLS_FILES), timeout=15)
        _need(_pin(_tls_files(raw)) == prepared.mtls_files_sha256, 'caddy_mtls_files_changed')
        _need(self._mtls_volume()[0] == prepared.mtls_volume_sha256, 'caddy_mtls_volume_changed')

    def prepare(self, baseline, active_ip, private_dir, *, expected_mtls_sha256):
        try:
            _need(type(expected_mtls_sha256) is dict and set(expected_mtls_sha256) == set(TLS_FILES)
                  and all(type(value) is str and ID.fullmatch(value) for value in expected_mtls_sha256.values()),
                  'caddy_mtls_fingerprint_required')
            current, raw, upstreams = self._snapshot()
            _need(current == baseline, 'caddy_baseline_changed')
            directory = _private_dir(private_dir)
            volume_sha, _ = self._mtls_volume()
            candidate = directory / 'Caddyfile'
            override = directory / 'caddy-ha-compose.json'
            data = render(raw, baseline['source_sha256'], active_ip)
            overlay = json.dumps({'services': {'caddy': {'image': baseline['image'], 'volumes': [
                self._bind(candidate, CONFIG), self._mtls_mount()]}},
                'volumes': {MTLS_VOLUME: {'external': True, 'name': MTLS_VOLUME}}}).encode()
            _write(candidate, data, 0o444)
            _write(override, overlay, 0o600)
            _sync_dir(directory)
            prepared = PreparedCaddy(copy.deepcopy(baseline), override, candidate, _sha(overlay), _sha(data), volume_sha, '')
            effective = self._prepared(prepared, upstreams)
            self._isolated(effective, prepared)
            result = self._docker('run', '--rm', '--pull=never', '--network=none', '--read-only',
                                  '--tmpfs', '/data', '--tmpfs', '/config',
                                  '--mount', f'type=bind,src={candidate},dst={CONFIG},readonly',
                                  '--mount', f'type=volume,src={MTLS_VOLUME},dst={MOUNT},readonly,volume-nocopy',
                                  '-e', 'CADDY_EMAIL=validation@example.invalid', '-e', 'CLOUDFLARE_API_TOKEN=validation',
                                  '--entrypoint', '/bin/sh', baseline['image'], '-ec',
                                  '/usr/bin/caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile >/dev/null\nexec sha256sum "$@"',
                                  'caddy-validate', *(MOUNT + '/' + name for name in TLS_FILES), timeout=45)
            observed_tls = _tls_files(result)
            _need(observed_tls == expected_mtls_sha256, 'caddy_mtls_files_changed')
            self.prepared = replace(prepared, mtls_files_sha256=_pin(observed_tls))
            return copy.deepcopy(self.prepared)
        except (OSError, KeyError, ValueError, TypeError, AttributeError):
            raise TransitionBlocked('caddy_prepare_unconfirmed') from None

    def apply(self, prepared, assert_guard):
        """Run only after the caller durably records LegacyCutover switch_caddy_intent."""
        _need(not self.attempted, 'caddy_reconciliation_required')
        _need(prepared == self.prepared, 'caddy_candidate_not_validated')
        try:
            assert_guard()
            current, _, upstreams = self._snapshot()
            _need(current == prepared.baseline, 'caddy_baseline_changed')
            effective = self._prepared(prepared, upstreams)
            isolated = self._isolated(effective, prepared)
            self._check_prepared_credentials(prepared)
            directory = _private_dir(prepared.override.parent)
            self.attempted = True
            _write(directory / 'caddy-apply-intent.json', json.dumps({
                'original_container_sha256': _sha(prepared.baseline['id'].encode()),
                'candidate_sha256': prepared.candidate_sha256,
                'overlay_sha256': prepared.overlay_sha256,
                'isolated_sha256': _sha(isolated),
            }).encode(), 0o600)
            _sync_dir(directory)
            assert_guard()
            self._compose(('-',), 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate',
                          'caddy', input_data=isolated, timeout=120)
            item = self._container(isolated=True)
            _need(item['Id'] != prepared.baseline['id'] and item['Image'] == prepared.baseline['image']
                  and _runtime_contract(item, (CONFIG, MOUNT)) == prepared.baseline['runtime_sha256'],
                  'caddy_replacement_contract_changed')
            volumes, mounts = self._mounts(item, (CONFIG, MOUNT))
            _need(volumes == prepared.baseline['volumes'], 'caddy_persistent_volume_changed')
            _need(self._network(self._config(prepared.baseline['compose_files'], upstreams), item) == prepared.baseline['network_sha256'],
                  'caddy_network_scope_changed')
            volume_sha, mountpoint = self._mtls_volume()
            tls = mounts[MOUNT]
            source = mounts[CONFIG]
            _need(volume_sha == prepared.mtls_volume_sha256 and tls['Type'] == 'volume' and tls['Name'] == MTLS_VOLUME
                  and tls['Driver'] == 'local' and tls['Source'] == mountpoint and tls['RW'] is False
                  and source['Type'] == 'bind' and source['Source'] == str(prepared.candidate) and source['RW'] is False,
                  'caddy_replacement_mount_unconfirmed')
            tls_options = [row for row in item['HostConfig'].get('Mounts') or [] if row['Target'] == MOUNT]
            _need(len(tls_options) == 1 and tls_options[0].get('VolumeOptions', {}).get('NoCopy') is True,
                  'caddy_replacement_mount_unconfirmed')
            tls_files = self._docker('exec', item['Id'], '/bin/busybox', 'sha256sum',
                                     *(MOUNT + '/' + name for name in TLS_FILES))
            _need(_pin(_tls_files(tls_files)) == prepared.mtls_files_sha256, 'caddy_mtls_files_changed')
            raw, live = self._live_config(item['Id'], starting=True)
            _need(_sha(raw) == prepared.candidate_sha256, 'caddy_live_config_unconfirmed')
            self._prepared(prepared, upstreams)
            assert_guard()
            return {'container_sha256': _sha(item['Id'].encode()), 'live_sha256': live,
                    'candidate_sha256': prepared.candidate_sha256}
        except TransitionBlocked:
            if not self.attempted:
                raise
            raise TransitionBlocked('caddy_reconciliation_required') from None
        except (OSError, KeyError, ValueError, TypeError, AttributeError):
            reason = 'caddy_reconciliation_required' if self.attempted else 'caddy_apply_preflight_unconfirmed'
            raise TransitionBlocked(reason) from None
