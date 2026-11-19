"""Replace the fixed-IP Car callback relay independently of main Caddy."""

from contextlib import nullcontext
from dataclasses import dataclass
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import time

from . import car_callback_ingress, caddy_runtime as caddy
from .controller import TransitionBlocked


NAME = 'n100-ha-car-callback-origin'
LABELS = {
    caddy.LABEL + 'project': 'n100-ha-car-relay',
    caddy.LABEL + 'service': 'car-callback-origin',
}
INTENT = 'car-relay-replacement-intent.json'


@dataclass(frozen=True)
class Relay:
    container_sha256: str
    image: str
    network: str
    address: str
    path: Path
    guest: str
    route_pin: str
    mtls_volume_sha256: str
    mtls_files_sha256: str

    def validate(self):
        caddy._need(caddy.ID.fullmatch(self.container_sha256) and caddy.IMAGE.fullmatch(self.image)
                    and all(caddy.ID.fullmatch(pin) for pin in (
                        self.network, self.mtls_volume_sha256, self.mtls_files_sha256)),
                    'car_relay_identity_unconfirmed')
        caddy._safe_path(self.path)
        address = IPv4Address(self.address)
        caddy._need(str(address) == self.address and address.is_private and not address.is_loopback
                    and not address.is_unspecified and not address.is_link_local and not address.is_reserved,
                    'car_relay_network_unconfirmed')


class CallbackRuntime:
    def __init__(self, docker, *, execution=None):
        self.docker = docker
        self.execution = execution

    def _one(self, *args):
        (item,) = json.loads(self.docker(*args))
        return item

    def _locate(self):
        ids = self.docker('ps', '-aq', '--no-trunc', '--filter', f'name=^/{NAME}$').decode('ascii').split()
        caddy._need(len(ids) == 1 and caddy.ID.fullmatch(ids[0]), 'car_relay_identity_unconfirmed')
        return ids[0], self._one('inspect', ids[0])

    def _inspect(self, relay, *, replacement):
        container_id, item = self._locate()
        return self._verify(container_id, item, relay, replacement=replacement)

    def _verify(self, container_id, item, relay, *, replacement):
        config, host, state = item['Config'], item['HostConfig'], item['State']
        mounts = {mount['Destination']: mount for mount in item['Mounts']}
        caddy._need(item['Id'] == container_id and item['Image'] == relay.image and item['Name'] == '/' + NAME
                    and state['Running'] is True and state['Status'] == 'running'
                    and not any(state.get(key) for key in ('Paused', 'Restarting', 'Dead', 'OOMKilled'))
                    and config['User'] == '10001:10001' and config['Entrypoint'] == ['/usr/bin/caddy']
                    and config['Cmd'] == ['run', '--config', caddy.CONFIG, '--adapter', 'caddyfile']
                    and host['ReadonlyRootfs'] is True and host['Privileged'] is False
                    and host['LogConfig']['Type'] == 'none' and not host['PortBindings']
                    and host['SecurityOpt'] in (['no-new-privileges'], ['no-new-privileges:true'])
                    and host['CapDrop'] == ['ALL'] and host['CapAdd'] in (['NET_BIND_SERVICE'], ['CAP_NET_BIND_SERVICE'])
                    and host['RestartPolicy']['Name'] == 'unless-stopped', 'car_relay_container_unconfirmed')
        if replacement:
            caddy._need(all(config['Labels'].get(key) == value for key, value in LABELS.items())
                        and config['Labels'].get('n100-ha.car-relay-route') == relay.route_pin,
                        'car_relay_compose_scope_unconfirmed')
        else:
            caddy._need(caddy._sha(container_id.encode()) == relay.container_sha256, 'car_relay_identity_unconfirmed')
        caddy._need(len(item['Mounts']) == 2 and set(mounts) == {caddy.CONFIG, caddy.MOUNT}
                    and all(mount['RW'] is False for mount in mounts.values())
                    and mounts[caddy.CONFIG]['Type'] == 'bind' and mounts[caddy.CONFIG]['Source'] == str(relay.path)
                    and mounts[caddy.MOUNT]['Type'] == 'volume' and mounts[caddy.MOUNT]['Name'] == caddy.MTLS_VOLUME,
                    'car_relay_mounts_unconfirmed')
        volume = self._one('volume', 'inspect', caddy.MTLS_VOLUME)
        fingerprints = self.docker('exec', container_id, '/bin/busybox', 'sha256sum',
                                   *(caddy.MOUNT + '/' + name for name in caddy.TLS_FILES))
        caddy._need(caddy._pin(volume) == relay.mtls_volume_sha256
                    and mounts[caddy.MOUNT]['Source'] == volume['Mountpoint']
                    and caddy._pin(caddy._tls_files(fingerprints)) == relay.mtls_files_sha256,
                    'car_relay_mtls_unconfirmed')
        (attachment,) = item['NetworkSettings']['Networks'].values()
        network = self._one('network', 'inspect', relay.network)
        caddy._need(attachment['NetworkID'] == relay.network and attachment['IPAddress'] == relay.address
                    and network['Id'] == relay.network and network['Driver'] == 'bridge' and network['Scope'] == 'local'
                    and set(network['Containers']) == {container_id}, 'car_relay_network_unconfirmed')
        raw = caddy._read(relay.path)
        caddy._need(raw == car_callback_ingress.render(relay.guest, route_pin=relay.route_pin)
                    and self.docker('exec', container_id, '/bin/cat', caddy.CONFIG) == raw,
                    'car_relay_route_unconfirmed')
        return container_id, raw

    def observe_initial(self, active, path, *, route_pin, route_sha256, started_sha256, network, address):
        """Bind the relay receipt, Caddy mTLS and exact running relay without changing it."""
        error = 'car_relay_initial_unconfirmed'
        caddy._need(type(active) is caddy.ActiveCaddy
                    and all(type(pin) is str and caddy.ID.fullmatch(pin)
                            for pin in (route_sha256, started_sha256)), error)
        try:
            container_id, item = self._locate()
            caddy._need(caddy._sha(item['State']['StartedAt'].encode()) == started_sha256, error)
            previous = active.previous
            relay = Relay(active.relay_container_sha256, previous.baseline['image'],
                          network, address, Path(path), active.primary_ip,
                          route_pin, previous.mtls_volume_sha256, previous.mtls_files_sha256)
            relay.validate()
            _, raw = self._verify(container_id, item, relay, replacement=False)
            caddy._need(caddy._sha(raw) == route_sha256, error)
            return relay
        except (OSError, KeyError, ValueError, TypeError, AttributeError):
            raise TransitionBlocked(error) from None

    @staticmethod
    def _mounted(path):
        return ('--mount', f'type=bind,source={path},target={caddy.CONFIG},readonly',
                '--mount', f'type=volume,source={caddy.MTLS_VOLUME},target={caddy.MOUNT},readonly,volume-nocopy')

    def _validate_candidate(self, relay):
        volume = self._one('volume', 'inspect', caddy.MTLS_VOLUME)
        caddy._need(caddy._pin(volume) == relay.mtls_volume_sha256, 'car_relay_mtls_unconfirmed')
        fingerprints = self.docker(
            'run', '--rm', '--pull=never', '--network=none', '--read-only', '--user', '10001:10001',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            *(arg for key, value in LABELS.items() for arg in ('--label', f'{key}={value}')),
            '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16m',
            '--tmpfs', '/data:rw,nosuid,nodev,noexec,uid=10001,gid=10001,mode=0700,size=16m',
            '--tmpfs', '/config:rw,nosuid,nodev,noexec,uid=10001,gid=10001,mode=0700,size=16m',
            *self._mounted(relay.path), '--entrypoint', '/bin/sh', relay.image, '-ec',
            '/usr/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null\n'
            'exec /bin/busybox sha256sum "$@"', 'car-relay-validate',
            *(caddy.MOUNT + '/' + name for name in caddy.TLS_FILES), timeout=30)
        caddy._need(caddy._pin(caddy._tls_files(fingerprints)) == relay.mtls_files_sha256,
                    'car_relay_mtls_unconfirmed')

    def replace(self, previous, guest, route_pin, directory, *, assert_guard, verify_origin=car_callback_ingress.verify_public_origin):
        """Perform one guarded relay replacement. An uncertain result is observed separately."""
        previous.validate()
        directory = caddy._private_dir(directory)
        intent = directory / INTENT
        caddy._need(not os.path.lexists(intent), 'car_relay_reconciliation_required')
        assert_guard()
        old, current = self._inspect(previous, replacement=False)
        candidate = car_callback_ingress.retarget(current, previous.guest, previous.route_pin, guest, route_pin)
        path = directory / 'Caddyfile'
        caddy._write(path, candidate, 0o444)
        target = Relay(previous.container_sha256, previous.image, previous.network, previous.address,
                       path, guest, route_pin, previous.mtls_volume_sha256, previous.mtls_files_sha256)
        self._validate_candidate(target)
        caddy._write(intent, json.dumps({
            'previous_container_sha256': previous.container_sha256,
            'source_sha256': caddy._sha(current), 'candidate_sha256': caddy._sha(candidate),
            'guest': guest, 'route_pin': route_pin,
        }, sort_keys=True).encode(), 0o600)
        caddy._sync_dir(directory)
        return self._advance(previous, directory, 'old_running', old, assert_guard, verify_origin)

    def _plan(self, previous, directory):
        previous.validate()
        directory = caddy._private_dir(directory)
        intent = json.loads(caddy._read(directory / INTENT))
        original = car_callback_ingress.render(previous.guest, route_pin=previous.route_pin)
        candidate = car_callback_ingress.retarget(original, previous.guest, previous.route_pin,
                                                  intent['guest'], intent['route_pin'])
        caddy._need(set(intent) == {'previous_container_sha256', 'source_sha256', 'candidate_sha256', 'guest', 'route_pin'}
                    and intent['previous_container_sha256'] == previous.container_sha256
                    and intent['source_sha256'] == caddy._sha(original)
                    and intent['candidate_sha256'] == caddy._sha(candidate), 'car_relay_intent_unconfirmed')
        relay = Relay(previous.container_sha256, previous.image, previous.network, previous.address,
                      directory / 'Caddyfile', intent['guest'], intent['route_pin'],
                      previous.mtls_volume_sha256, previous.mtls_files_sha256)
        caddy._need(caddy._read(relay.path) == candidate, 'car_relay_candidate_changed')
        return relay, candidate

    def _phase(self, previous, relay):
        ids = self.docker('ps', '-aq', '--no-trunc', '--filter', f'name=^/{NAME}$').decode('ascii').split()
        network = self._one('network', 'inspect', previous.network)
        caddy._need(network['Id'] == previous.network and network['Driver'] == 'bridge'
                    and network['Scope'] == 'local', 'car_relay_network_unconfirmed')
        if not ids:
            caddy._need(not network['Containers'], 'car_relay_network_unconfirmed')
            return 'vacant', None
        caddy._need(len(ids) == 1 and caddy.ID.fullmatch(ids[0]), 'car_relay_identity_unconfirmed')
        item = self._one('inspect', ids[0])
        caddy._need(item['Id'] == ids[0] and item['Name'] == '/' + NAME and item['Image'] == previous.image,
                    'car_relay_identity_unconfirmed')
        old = caddy._sha(ids[0].encode()) == previous.container_sha256
        if item['State']['Running'] is True:
            self._inspect(previous if old else relay, replacement=not old)
            return ('old_running' if old else 'new_running'), ids[0]
        if old and item['State']['Running'] is False and item['State']['Status'] == 'exited':
            caddy._need(not network['Containers'], 'car_relay_network_unconfirmed')
            return 'old_stopped', ids[0]
        raise TransitionBlocked('car_relay_reconciliation_required')

    def status(self, previous, directory, *, assert_guard):
        """Read the exact relay and fixed-IP network after a partial replacement."""
        relay, _ = self._plan(previous, directory)
        assert_guard()
        phase, _ = self._phase(previous, relay)
        return phase

    def resume(self, previous, directory, *, assert_guard, assert_settled,
               verify_origin=car_callback_ingress.verify_public_origin):
        """Finish only a settled phase. The caller proves daemon-side settlement."""
        relay, _ = self._plan(previous, directory)
        assert_guard()
        assert_settled()
        phase, container = self._phase(previous, relay)
        if phase == 'new_running':
            return self.observe(previous, directory, assert_guard=assert_guard, verify_origin=verify_origin)
        return self._advance(previous, directory, phase, container, assert_guard, verify_origin, settled=True)

    def _advance(self, previous, directory, phase, container, assert_guard, verify_origin, *, settled=False):
        try:
            relay, candidate = self._plan(previous, directory)
            assert_guard()
            caddy._need(self._phase(previous, relay) == (phase, container), 'car_relay_active_changed')
            if phase == 'old_running':
                witness = self.execution.stop_relay(directory, container, settled=settled) if self.execution else nullcontext()
                with witness:
                    self.docker('stop', '--time', '5', container, timeout=15)
                phase = 'old_stopped'
            if phase == 'old_stopped':
                assert_guard()
                caddy._need(self._phase(previous, relay) == (phase, container), 'car_relay_stop_unconfirmed')
                self.docker('rm', container)
            assert_guard()
            caddy._need(self._phase(previous, relay) == ('vacant', None)
                        and caddy._read(relay.path) == candidate, 'car_relay_candidate_changed')
            self._validate_candidate(relay)
            assert_guard()
            caddy._need(caddy._read(relay.path) == candidate, 'car_relay_candidate_changed')
            self.docker('run', '--detach', '--pull=never', '--name', NAME, '--restart', 'unless-stopped',
                        '--network', relay.network, '--ip', relay.address,
                        '--read-only', '--cap-drop', 'ALL', '--cap-add', 'NET_BIND_SERVICE', '--user', '10001:10001',
                        '--security-opt', 'no-new-privileges', '--log-driver', 'none',
                        *(arg for key, value in LABELS.items() for arg in ('--label', f'{key}={value}')),
                        '--label', f'n100-ha.car-relay-route={relay.route_pin}',
                        *self._mounted(relay.path), '--entrypoint', '/usr/bin/caddy', relay.image,
                        'run', '--config', caddy.CONFIG, '--adapter', 'caddyfile', timeout=20)
        except (TransitionBlocked, OSError, ValueError, TypeError, KeyError):
            raise TransitionBlocked('car_relay_reconciliation_required') from None
        return self.observe(previous, directory, assert_guard=assert_guard, verify_origin=verify_origin)

    def observe(self, previous, directory, *, assert_guard, verify_origin=car_callback_ingress.verify_public_origin):
        """Resolve an uncertain reply from exact local and public state, without retrying mutation."""
        relay, candidate = self._plan(previous, directory)
        assert_guard()
        new, raw = self._inspect(relay, replacement=True)
        caddy._need(caddy._sha(new.encode()) != previous.container_sha256 and raw == candidate, 'car_relay_not_replaced')
        public = []
        for index in range(3):
            if index:
                time.sleep(10)
            assert_guard()
            public.append(verify_origin(relay.route_pin))
        assert_guard()
        caddy._need(len(set(public)) == 3 and all(caddy.ID.fullmatch(pin) for pin in public)
                    and self._inspect(relay, replacement=True) == (new, raw), 'car_relay_public_unconfirmed')
        return {'container_sha256': caddy._sha(new.encode()), 'candidate_sha256': caddy._sha(raw),
                'public_proof_sha256': public}
