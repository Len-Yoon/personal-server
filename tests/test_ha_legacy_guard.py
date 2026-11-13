"""Source-bound guard composition and native bridge protocol; no remote actions."""
import base64
import ast
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import queue
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra/k8s"))
from ha_control import legacy_guard as guard
from ha_control.controller import TransitionBlocked
from ha_control.windows import WindowsFence


class LegacyGuardTests(unittest.TestCase):
    def setUp(self):
        self.observation = dict(schema_version=1, complete=True, boot_ticks="123456",
                                principal_sha256="a" * 64, services=[], tasks=[], processes=[],
                                startup_sha256="b" * 64)
        self.guard = dict(schema_version=1, windows=dict(
            schema_version=1, boot_ticks="123456", principal_sha256="a" * 64,
            inventory_sha256=guard.digest(self.observation), frozen_services=[], frozen_tasks=[],
            provider_services=[], provider_tasks=[], provider_processes=[]),
            linux=dict(distribution="Fixture-Linux", reader_user="root", spec={"uid": 0}))
        self.plan = dict(source_commit="a" * 40, cluster="fixture", generation=7,
                         legacy_guard=self.guard, legacy_linux_sha256="c" * 64,
                         legacy_guard_code_sha256="d" * 64)
        self.request = dict(schema_version=1, nonce="e" * 64, plan_sha256="0" * 64,
                            cluster="fixture", generation=7, collector_sha256="c" * 64,
                            guard=self.guard, windows=self.observation, linux_receipt={})
        self.seal()
        self.source = patch.object(guard, "__collector_sha256__", "d" * 64, create=True)
        self.source.start()
        self.addCleanup(self.source.stop)

    def seal(self):
        raw = json.dumps(self.plan).encode()
        self.request["plan_sha256"] = hashlib.sha256(raw).hexdigest()
        self.request["plan_raw"] = base64.b64encode(raw).decode()
        self.approval = dict(action="ha-transition", source_commit="a" * 40,
                             plan_sha256=self.request["plan_sha256"],
                             expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
        self.request["approval_raw"] = base64.b64encode(json.dumps(self.approval).encode()).decode()
        self.request["linux_receipt"] = {key: self.request[key] for key in (
            "schema_version", "nonce", "plan_sha256", "cluster", "generation", "collector_sha256")}
        self.request["linux_receipt"].update(complete=True, verified=True)

    def reject(self):
        with self.assertRaises(guard.GuardBlocked):
            guard.verify(self.request)

    def test_complete_live_receipt_is_bound_to_exact_plan_and_approval(self):
        self.assertEqual(guard.verify(self.request), self.request["linux_receipt"])

    def test_actual_isolated_bootstrap_executes_only_the_pinned_source_bytes(self):
        directory = Path(guard.__file__).parent
        source = (directory / "legacy_guard.py").read_bytes()
        source_sha = hashlib.sha256(source).hexdigest()
        self.plan["legacy_guard_code_sha256"] = source_sha
        self.seal()
        line = next(line for line in (directory / "windows-legacy-guard.ps1").read_text().splitlines()
                    if line.strip().startswith("$bootstrap = "))
        bootstrap = ast.literal_eval(line.split(" = ", 1)[1])
        envelope = dict(code=base64.b64encode(source).decode(), sha256=source_sha, request=self.request)
        result = subprocess.run([sys.executable, "-I", "-B", "-S", "-c", bootstrap],
                                input=json.dumps(envelope), capture_output=True, text=True, timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.request["linux_receipt"])
        envelope["code"] = base64.b64encode(source + b"\n").decode()
        result = subprocess.run([sys.executable, "-I", "-B", "-S", "-c", bootstrap],
                                input=json.dumps(envelope), capture_output=True, text=True, timeout=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('"verified": true', result.stdout)

    def test_missing_source_binding_cannot_compare_as_two_nulls(self):
        self.plan["legacy_guard_code_sha256"] = None
        self.seal()
        with patch.object(guard, "__collector_sha256__", None):
            self.reject()

    def test_receipt_cannot_replay_nonce_plan_cluster_or_generation(self):
        for key, value in (("nonce", "f" * 64), ("plan_sha256", "f" * 64),
                           ("cluster", "other"), ("generation", 8), ("collector_sha256", "f" * 64)):
            request = deepcopy(self.request)
            self.request["linux_receipt"][key] = value
            self.reject()
            self.request = request

    def test_boolean_generation_and_incomplete_or_extended_receipts_are_denied(self):
        for key, value in (("schema_version", True), ("complete", 1), ("verified", False), ("unknown", 1)):
            receipt = dict(self.request["linux_receipt"])
            self.request["linux_receipt"][key] = value
            self.reject()
            self.request["linux_receipt"] = receipt
        self.request["generation"] = True
        self.reject()

    def test_raw_plan_and_approval_tampering_is_denied(self):
        for field in ("plan_raw", "approval_raw"):
            original = self.request[field]
            self.request[field] = base64.b64encode(b"{}").decode()
            self.reject()
            self.request[field] = original

    def test_duplicate_plan_keys_are_denied_even_when_bytes_are_pinned(self):
        raw = json.dumps(self.plan).encode()
        raw = raw[:-1] + b',"generation":7}'
        self.request["plan_raw"] = base64.b64encode(raw).decode()
        self.request["plan_sha256"] = hashlib.sha256(raw).hexdigest()
        self.reject()

    def test_expired_approval_and_wrong_commit_are_denied(self):
        for key, value in (("expires_at", "2020-01-01T00:00:00Z"), ("source_commit", "f" * 40), ("action", "observe")):
            approval = dict(self.approval, **{key: value})
            self.request["approval_raw"] = base64.b64encode(json.dumps(approval).encode()).decode()
            self.reject()

    def test_windows_snapshot_change_is_not_auto_approved(self):
        self.observation["startup_sha256"] = "f" * 64
        self.reject()

    def test_approved_running_recreator_still_cannot_pass(self):
        row = dict(id="1" * 64, spec_sha256="2" * 64, creator=True, provider=False, disabled=False, stopped=False)
        self.observation["services"] = [row]
        self.guard["windows"]["frozen_services"] = [row["id"]]
        self.guard["windows"]["inventory_sha256"] = guard.digest(self.observation)
        self.seal()
        self.reject()
        row.update(disabled=True, stopped=True)
        self.guard["windows"]["inventory_sha256"] = guard.digest(self.observation)
        self.seal()
        guard.verify(self.request)

    def test_unregistered_recreator_and_duplicate_service_ids_are_denied(self):
        for rows in ([dict(id="1" * 64, spec_sha256="2" * 64, creator=True, provider=False, disabled=True, stopped=True)],
                     [dict(id="1" * 64, spec_sha256="2" * 64, creator=False, provider=True, disabled=True, stopped=True)] * 2):
            self.observation["services"] = rows
            self.guard["windows"]["inventory_sha256"] = guard.digest(self.observation)
            self.seal()
            self.reject()

    def test_incomplete_windows_observation_is_denied_even_when_pinned(self):
        self.observation["complete"] = False
        self.guard["windows"]["inventory_sha256"] = guard.digest(self.observation)
        self.seal()
        self.reject()

    def test_default_reader_is_not_silently_promoted_to_root(self):
        self.guard["linux"]["spec"]["uid"] = 1000
        self.seal()
        self.reject()

    def test_registered_unknown_client_is_not_a_trusted_os_provider(self):
        row = dict(id="1" * 64, spec_sha256="2" * 64, creator=False, provider=False)
        self.observation["processes"] = [row]
        self.guard["windows"]["provider_processes"] = [row["id"]]
        self.guard["windows"]["inventory_sha256"] = guard.digest(self.observation)
        self.seal()
        self.reject()
        row["provider"] = True
        self.guard["windows"]["inventory_sha256"] = guard.digest(self.observation)
        self.seal()
        guard.verify(self.request)

    def test_verified_provider_not_in_approval_is_denied(self):
        self.observation["processes"] = [dict(id="1" * 64, spec_sha256="2" * 64, creator=False, provider=True)]
        self.guard["windows"]["inventory_sha256"] = guard.digest(self.observation)
        self.seal()
        self.reject()


class WindowsBridgeProofTests(unittest.TestCase):
    def test_reactivation_input_walk_uses_dotnet_file_types_under_strict_mode(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "infra/k8s/ha_control/windows-reactivation-entry.ps1").read_text()
        self.assertIn("Set-StrictMode -Version Latest", script)
        self.assertIn("$cursor -is [IO.DirectoryInfo]", script)
        self.assertIn("$cursor -is [IO.FileInfo]", script)
        self.assertNotIn("$cursor.PSIsContainer", script)

    def setUp(self):
        self.bridge = WindowsFence.__new__(WindowsFence)
        self.bridge.process = Mock()
        self.bridge.process.poll.return_value = None
        self.bridge.process.stdin = io.StringIO()
        self.bridge.answers = queue.Queue()
        self.bridge.plan_sha256 = "a" * 64
        self.bridge.plan_data = dict(cluster="fixture", legacy_linux_sha256="b" * 64)
        self.state = dict(old="A", generation=7)
        self.nonce = patch("ha_control.windows.secrets.token_hex", return_value="c" * 64)
        self.nonce.start()
        self.addCleanup(self.nonce.stop)
        self.answer = dict(nonce="c" * 64, node="A", generation=7, status="ok", fenced=True,
                           legacy_guard=dict(schema_version=1, nonce="c" * 64, plan_sha256="a" * 64,
                                             cluster="fixture", generation=7, collector_sha256="b" * 64,
                                             complete=True, verified=True))

    def ask(self):
        self.bridge.answers.put(json.dumps(self.answer))
        return self.bridge.fenced(self.state)

    def test_exact_live_protocol_succeeds(self):
        self.assertTrue(self.ask())

    def test_old_protocol_without_legacy_guard_is_denied(self):
        del self.answer["legacy_guard"]
        with self.assertRaises(TransitionBlocked): self.ask()

    def test_boolean_schema_or_wrong_collector_cannot_pass(self):
        for key, value in (("schema_version", True), ("collector_sha256", "f" * 64), ("nonce", "f" * 64)):
            original = self.answer["legacy_guard"][key]
            self.answer["legacy_guard"][key] = value
            with self.assertRaises(TransitionBlocked): self.ask()
            self.answer["legacy_guard"][key] = original

    def test_end_to_end_observe_deadline_includes_transport(self):
        with patch("ha_control.windows.time.monotonic", side_effect=[10, 12.001]):
            with self.assertRaises(TransitionBlocked): self.ask()

    def test_non_object_and_duplicate_key_responses_have_static_failure(self):
        for raw in ('[]', '{"nonce":"a","nonce":"b"}'):
            self.bridge.answers.put(raw)
            with self.assertRaisesRegex(TransitionBlocked, "windows_fence_unconfirmed"):
                self.bridge.fenced(self.state)




class NativeLinuxWritersTests(unittest.TestCase):
    def test_fence_waits_for_controller_and_rollback_preserves_original_writers(self):
        for agent_running in (True, False):
            with self.subTest(agent_running=agent_running):
                self._exercise_fence_and_rollback(agent_running)

    def _exercise_fence_and_rollback(self, agent_running):
        from ha_control.legacy_writers import COMPOSE_ORDER, K3S_SERVICES, LinuxWriters

        deployments = {}
        schedules = []
        containers = {}
        for index, service in enumerate(sorted(K3S_SERVICES), 1):
            deployments[service] = dict(namespace='personal-server', name=service,
                                        uid=f'00000000-0000-0000-0000-{index:012}', replicas=1)
            schedules.append(dict(namespace='personal-server', name=f'backup-{index}',
                                  uid=f'00000000-0000-0000-0001-{index:012}', suspend=False))
        for index, service in enumerate(COMPOSE_ORDER, 1):
            containers[service] = dict(id=f'{index:064x}', image='sha256:' + 'a' * 64,
                                       running=service not in K3S_SERVICES, restart='unless-stopped')
        containers['system-agent']['running'] = agent_running
        baseline = dict(deployments=deployments, schedules=schedules, containers=containers)
        active = {row['id']: (row['running'], row['restart']) for row in containers.values()}
        commands = []

        def docker(args, timeout=10):
            command = args[3]
            cid = args[-1]
            running, restart = active[cid]
            commands.append((command, cid))
            active[cid] = {
                'update': (running, args[-2].split('=', 1)[1]) if command == 'update' else (running, restart),
                'stop': (False, restart),
                'start': (True, restart),
            }[command]

        writer = LinuxWriters(baseline, docker)
        containers['homeops-executor']['running'] = False
        actual = writer._baseline
        states = {row['uid']: row['replicas'] for row in deployments.values()}
        states.update({row['uid']: row['suspend'] for row in schedules})

        def patch_state(_kind, row, _field, _current, desired, _resource):
            states[row['uid']] = desired

        def container_state(service):
            return active[actual['containers'][service]['id']]

        deployment = actual['deployments']['portal-web']
        uid = deployment['uid']
        replicas = dict(kind='ReplicaSet', metadata=dict(uid='rs', ownerReferences=[
            dict(kind='Deployment', uid=uid, controller=True)]), spec=dict(replicas=1))
        with patch.object(writer, '_objects', return_value=[replicas]):
            self.assertTrue(writer._writers_active({'app-data'}, include_deployments=True))
        with (patch.object(writer, '_no_autoscaler'),
              patch.object(writer, '_assert_schedule_graph', return_value={'app-data'}),
              patch.object(writer, '_container', side_effect=container_state),
              patch.object(writer, '_deployment', side_effect=lambda row: (
                  dict(metadata=dict(generation=1), status=dict(
                      observedGeneration=1, replicas=states[row['uid']], readyReplicas=states[row['uid']])),
                  states[row['uid']])),
              patch.object(writer, '_schedule', side_effect=lambda row: ({}, states[row['uid']])),
              patch.object(writer, '_patch', side_effect=patch_state),
              patch.object(writer, '_writers_active', return_value=False)):
            writer.block_reactivation()
            if not agent_running:
                agent_id = actual['containers']['system-agent']['id']
                active[agent_id] = (True, 'no')
                with self.assertRaisesRegex(TransitionBlocked, 'legacy_writer_reactivation_not_blocked'):
                    writer.fence()
                active[agent_id] = (False, 'no')
            writer.fence()
            self.assertEqual([cid for name, cid in commands if name == 'stop'],
                             [row['id'] for row in actual['containers'].values() if row['running']])
            self.assertTrue(all(state == (False, 'no') for state in active.values()))
            writer.restore()
            for service, row in actual['containers'].items():
                self.assertEqual(active[row['id']], (row['running'], row['restart']))
            if not agent_running:
                self.assertNotIn(('start', actual['containers']['system-agent']['id']), commands)
            self.assertTrue(all(states[row['uid']] is False for row in schedules))
            self.assertTrue(all(states[row['uid']] == 1 for row in deployments.values()))


class NativePvcCaptureTests(unittest.TestCase):
    def test_extract_uses_private_files_and_rejects_link_members(self):
        import io
        import tarfile
        import tempfile
        from ha_control.pvc_capture import _extract

        def archive(link=False):
            result = io.BytesIO()
            with tarfile.open(fileobj=result, mode="w") as writer:
                entry = tarfile.TarInfo("v0")
                entry.type = tarfile.DIRTYPE
                writer.addfile(entry)
                entry = tarfile.TarInfo("v0/state.db")
                if link:
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = "../outside"
                    writer.addfile(entry)
                else:
                    entry.size = 2
                    writer.addfile(entry, io.BytesIO(b"DB"))
            result.seek(0)
            return result

        with tempfile.TemporaryDirectory() as root:
            target = Path(root)
            _extract(archive(), target, 1)
            data = target / "v0/state.db"
            self.assertEqual(data.read_bytes(), b"DB")
            self.assertEqual(data.stat().st_mode & 0o777, 0o600)
            data.unlink()
            with self.assertRaisesRegex(TransitionBlocked, "pvc_capture_archive_member_invalid"):
                _extract(archive(link=True), target, 1)
            self.assertFalse(data.is_symlink())


    def test_reader_failure_waits_for_uid_bound_cleanup(self):
        from ha_control.pvc_capture import CLAIMS, PvcCapture

        uid = '00000000-0000-0000-0000-000000000001'
        image = 'fixture/image@sha256:' + 'a' * 64
        bindings = {
            service: dict(deployment_uid=uid, template_sha256='d' * 64, node='fixture', image=image, image_id=image,
                          claims=[dict(name=name, uid=uid, pv='fixture-pv', pv_uid=uid,
                                       pv_spec_sha256='b' * 64) for name in claims])
            for service, claims in CLAIMS.items()
        }
        calls = []

        def kubectl(args, *, body=None, **kwargs):
            calls.append(args)
            if 'create' in args:
                pod = json.loads(body)
                pod['metadata']['uid'] = uid
                return json.dumps(pod).encode()
            if 'delete' in args:
                self.assertEqual(json.loads(body)['preconditions'], {'uid': uid})
            return b''

        capture = PvcCapture(bindings, 'c' * 64, run=kubectl)
        with patch.object(capture, '_verify_running_reader'):
            with self.assertRaisesRegex(RuntimeError, 'read failed'):
                with capture._reader('book-memo'):
                    raise RuntimeError('read failed')
        self.assertIn('--dry-run=server', calls[0])
        self.assertIn('delete', calls[-2])
        self.assertIn('--ignore-not-found', calls[-1])
        self.assertEqual(sum('delete' in call for call in calls), 1)

        def replaced(args, **kwargs):
            if 'get' in args:
                return b'{"metadata":{"uid":"foreign","deletionTimestamp":"now"}}'
            return kubectl(args, **kwargs)

        capture.run = replaced
        with patch.object(capture, '_verify_running_reader'):
            with self.assertRaisesRegex(TransitionBlocked, 'pvc_capture_reader_archive_exec_cleanup_unconfirmed'):
                with capture._reader('book-memo'):
                    raise TransitionBlocked('pvc_capture_reader_archive_exec')
        with self.assertRaisesRegex(TransitionBlocked, 'pvc_capture_cleanup_reconciliation_required'):
            capture.assert_settled()


class CaddyRuntimeTests(unittest.TestCase):
    def setUp(self):
        from ha_control import caddy_runtime as caddy

        self.caddy = caddy
        self.root = Path(__file__).resolve().parents[1]
        self.work = self.enterContext(tempfile.TemporaryDirectory(dir=self.root))
        self.calls = []
        self.applied = False
        self.lose_apply_result = False
        self.drift = False
        self.live_service_hash = 'a' * 64
        self.desired_service_hash = self.live_service_hash
        self.upstreams = {key: f'fixture-service:{8000 + i}' for i, key in enumerate(caddy.UPSTREAMS)}
        self.network_id = 'f' * 64
        self.network_ambiguous = False
        self.image = 'sha256:' + 'a' * 64
        self.volume_names = {'/data': 'fixture_caddy_data', '/config': 'fixture_caddy_config',
                             caddy.MOUNT: caddy.MTLS_VOLUME}
        self.base = {'name': 'fixture', 'services': {'caddy': {'networks': {'default': None}, 'volumes': [
            {'type': 'volume', 'source': 'caddy_data', 'target': '/data'},
            {'type': 'volume', 'source': 'caddy_config', 'target': '/config'}]},
            'unrelated': {'image': 'pinned'}},
            'volumes': {key: {'name': f'fixture_{key}'} for key in ('caddy_data', 'caddy_config')},
            'networks': {'default': {'name': 'fixture_default', 'ipam': {}}, 'unrelated': {'name': 'untouched'}}}
        self.source = (self.root / 'caddy/Caddyfile').read_bytes()
        self.expected_tls = dict.fromkeys(caddy.TLS_FILES, '0' * 64)
        self.tls_files = ''.join(f'{sha}  {caddy.MOUNT}/{file}\n' for file, sha in self.expected_tls.items()).encode()
        self.runtime = caddy.CaddyRuntime(self.root, 'fixture', run=self.run_docker)

    def volume(self, name):
        return {'Name': name, 'Driver': 'local', 'Scope': 'local', 'Options': None,
                'Mountpoint': '/fixture/volumes/' + name,
                'Labels': {'com.docker.compose.project': 'fixture',
                           'com.docker.compose.volume': name.removeprefix('fixture_')}}

    def container(self):
        caddy = self.caddy
        identifier = ('2' if self.applied else '1') * 64
        files = [str(self.root / '-')] if self.applied else [str(self.root / file) for file in caddy.COMPOSE]
        config = dict(Hostname=identifier[:12], Image=self.image if self.applied else 'fixture-caddy',
                      Env=[f'{key}={value}' for key, value in self.upstreams.items()],
                      Labels={caddy.LABEL + key: value for key, value in {
                          'project': 'fixture', 'service': 'caddy', 'oneoff': 'False', 'container-number': '1',
                          'config-hash': self.live_service_hash,
                          'project.working_dir': str(self.root), 'project.config_files': ','.join(files)}.items()})
        ports = {f'{port}/tcp': [{'HostPort': port, 'HostIp': '0.0.0.0'}] for port in ('80', '443')}
        mounts = [{'Destination': path, 'Type': 'volume', 'Name': name, 'RW': path != caddy.MOUNT,
                   'Driver': 'local', 'Source': self.volume(name)['Mountpoint']}
                  for path, name in self.volume_names.items() if path != caddy.MOUNT or self.applied]
        host_mounts = []
        if self.applied:
            mounts.append({'Destination': caddy.CONFIG, 'Type': 'bind',
                           'RW': False, 'Source': str(Path(self.work) / 'Caddyfile')})
            host_mounts = [{'Target': caddy.MOUNT, 'VolumeOptions': {'NoCopy': True}}, {'Target': caddy.CONFIG}]
            if self.drift:
                mounts[-1]['RW'] = True
        return {'Id': identifier, 'Image': self.image, 'Config': config, 'Mounts': mounts,
                'State': {'Running': True, 'Status': 'running', 'StartedAt': 'start'},
                'HostConfig': {'Mounts': host_mounts, 'NetworkMode': 'fixture_default'},
                'NetworkSettings': {'Ports': ports, 'Networks': {'fixture_default': {'NetworkID': self.network_id}}}}

    def run_docker(self, args, **kwargs):
        args = args[len(self.caddy.DOCKER):]
        self.calls.append(args)
        result = None
        if args[0] == 'ps':
            return self.container()['Id'].encode()
        if args[0] == 'inspect':
            result = [self.container()]
        if args[:2] == ('image', 'inspect'):
            result = [{'Id': self.image}]
        if args[:2] == ('volume', 'inspect'):
            result = [self.volume(args[2])]
        if args[:2] == ('network', 'inspect'):
            self.assertEqual(args[2], self.network_id)
            result = [{'Id': self.network_id, 'Name': 'fixture_default', 'Scope': 'local', 'Driver': 'bridge',
                       'IPAM': {'Driver': 'default'}, 'Labels': {'com.docker.compose.project': 'fixture',
                                                                'com.docker.compose.network': 'default'}}]
        if args[:2] == ('network', 'ls'):
            ids = [self.network_id] + (['0' * 64] if self.network_ambiguous else [])
            return '\n'.join(ids).encode()
        if args[0] == 'exec':
            if args[2] == '/bin/cat':
                return (Path(self.work) / 'Caddyfile').read_bytes() if self.applied else self.source
            if args[2] == '/bin/busybox':
                return self.tls_files
            result = {'apps': {'http': {'routes': 'candidate' if self.applied else 'source'}}}
        if args[0] == 'run':
            self.assertIn('type=volume,src=n100-ha-caddy-mtls,dst=/run/n100-ha/caddy-mtls,readonly,volume-nocopy', args)
            self.assertIn('--network=none', args)
            if 'caddy-validate' in args:
                self.assertEqual(args[args.index('--user') + 1], '10001:10001')
                self.assertEqual(args[args.index('--cap-drop') + 1], 'ALL')
                self.assertEqual(args[args.index('--cap-add') + 1], 'NET_BIND_SERVICE')
                self.assertIn('/data:rw,nosuid,nodev,noexec,uid=10001,gid=10001,mode=0700,size=16m', args)
                self.assertIn('/config:rw,nosuid,nodev,noexec,uid=10001,gid=10001,mode=0700,size=16m', args)
                self.assertIn('CLOUDFLARE_API_TOKEN=validation-only-00000000000000000000000000000000', args)
            return self.tls_files
        if args[0] == 'compose':
            isolated = kwargs.get('input_data')
            if isolated is None:
                self.assertEqual(kwargs.get('compose_env'), self.upstreams)
            if args[-3:] == ('config', '--hash', 'caddy'):
                return f'caddy {self.desired_service_hash}\n'.encode()
            result = json.loads(isolated) if isolated is not None else deepcopy(self.base)
            if any(arg.endswith('caddy-ha-compose.json') for arg in args):
                self.override = Path(next(arg for arg in args if arg.endswith('caddy-ha-compose.json')))
                overlay = json.loads(self.override.read_bytes())
                result['services']['caddy']['image'] = overlay['services']['caddy']['image']
                result['services']['caddy']['volumes'] += overlay['services']['caddy']['volumes']
                result['volumes'].update(overlay['volumes'])
            if 'up' in args:
                self.assertIsNotNone(isolated)
                self.assertEqual(set(result['services']), {'caddy'})
                self.assertEqual(result['networks'], {'default': {'name': 'fixture_default', 'external': True}})
                self.assertTrue(all(value['external'] is True for value in result['volumes'].values()))
                self.assertEqual(args[-8:], ('up', '-d', '--no-deps', '--no-build', '--pull', 'never',
                                             '--force-recreate', 'caddy'))
                self.applied = True
                if self.lose_apply_result:
                    raise subprocess.TimeoutExpired(args, 1)
                return b''
        self.assertIsNotNone(result, args)
        return json.dumps(result).encode()

    def prepare(self):
        return self.runtime.prepare(self.runtime.inspect(), '172.20.0.10', self.work,
                                    expected_mtls_sha256=self.expected_tls)

    def test_apply_uses_only_pinned_caddy_and_preserves_persistent_volumes(self):
        prepared = self.prepare()
        guard = Mock()
        result = self.runtime.apply(prepared, guard)
        self.assertTrue(self.applied)
        self.assertEqual(guard.call_count, 3)
        self.assertEqual(result['candidate_sha256'], prepared.candidate_sha256)
        self.assertTrue((Path(self.work) / 'caddy-apply-intent.json').exists())
        self.assertEqual(len([cmd for cmd in self.calls if 'up' in cmd]), 1)

    def test_uncertain_apply_requires_reconciliation_without_resending(self):
        prepared = self.prepare()
        self.lose_apply_result = True
        for _ in range(2):
            with self.assertRaisesRegex(TransitionBlocked, 'caddy_reconciliation_required'):
                self.runtime.apply(prepared, Mock())
        self.assertEqual(len([cmd for cmd in self.calls if 'up' in cmd]), 1)

    def test_guard_loss_after_durable_intent_blocks_replacement(self):
        prepared = self.prepare()
        intent = Path(self.work) / 'caddy-apply-intent.json'

        def guard():
            if intent.is_file():
                raise TransitionBlocked('guard_lost')

        with self.assertRaisesRegex(TransitionBlocked, 'caddy_reconciliation_required'):
            self.runtime.apply(prepared, guard)
        self.assertTrue(intent.is_file())
        self.assertFalse(self.applied)

    def test_live_mount_drift_cannot_report_success(self):
        prepared = self.prepare()
        self.drift = True
        with self.assertRaisesRegex(TransitionBlocked, 'caddy_reconciliation_required'):
            self.runtime.apply(prepared, Mock())
        self.assertTrue(self.applied)

    def test_live_compose_drift_is_rejected_before_replacement(self):
        prepared = self.prepare()
        self.desired_service_hash = 'b' * 64
        with self.assertRaisesRegex(TransitionBlocked, 'caddy_live_compose_drift'):
            self.runtime.apply(prepared, Mock())
        self.assertFalse(self.applied)

    def test_credential_file_drift_is_rejected_before_replacement(self):
        prepared = self.prepare()
        self.tls_files = self.tls_files.replace(b'0' * 64, b'1' * 64, 1)
        with self.assertRaisesRegex(TransitionBlocked, 'caddy_mtls_files_changed'):
            self.runtime.apply(prepared, Mock())
        self.assertFalse(self.applied)

    def test_preparation_rejects_mtls_fingerprint_different_from_staged_receipt(self):
        self.expected_tls['client.key'] = '1' * 64
        with self.assertRaisesRegex(TransitionBlocked, 'caddy_mtls_files_changed'):
            self.prepare()
        self.assertFalse(self.applied)

    def test_project_replica_drift_is_rejected_before_replacement(self):
        self.base['services']['caddy']['deploy'] = {'replicas': 2}
        with self.assertRaisesRegex(TransitionBlocked, 'caddy_replica_scope_changed'):
            self.prepare()
        self.assertFalse(self.applied)

    def test_ambiguous_network_name_is_rejected_before_replacement(self):
        prepared = self.prepare()
        self.network_ambiguous = True
        with self.assertRaisesRegex(TransitionBlocked, 'caddy_network_name_ambiguous'):
            self.runtime.apply(prepared, Mock())
        self.assertFalse(self.applied)


class NativeApplicationsTests(unittest.TestCase):
    def run_attempt(self, failure=None):
        from ha_control import native_applications as module

        plan = '1' * 64
        trace = []
        calls = {}
        self.trace = trace
        self.calls = calls
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        journal = root / 'cutover.json'
        self.journal = journal

        @contextmanager
        def hold(*unused):
            yield

        def evidence(stage, plan, attempt, **facts):
            trace.append(stage)
            value = hashlib.sha256(f'{stage}:{len(trace)}'.encode()).hexdigest()
            calls[stage] = value
            return dict(plan_sha256=plan, attempt_id=attempt, evidence_sha256=value, **facts)

        class Capture:
            def __init__(self, base, identity, attempt, *unused):
                self.root = base / attempt
                self.identity, self.attempt = identity, attempt

            def prepare(self):
                self.root.mkdir(mode=0o700)
                trace.append('prepare_capture')
                return '2' * 64

            def block_reactivation(self):
                trace.append('block_linux')

            def fence(self, reactivation, settlement):
                return evidence('fence', self.identity['plan_sha256'], self.attempt,
                                reactivation_sha256=reactivation, settlement_sha256=settlement,
                                writers_closed=True, services=module.ALL_SERVICES)

            def capture(self, fence):
                return evidence('capture', self.identity['plan_sha256'], self.attempt,
                                fence_sha256=fence, manifest_sha256='3' * 64)

            def assert_closed(self, fence):
                if fence != calls['fence']:
                    raise TransitionBlocked('fixture_wrong_fence')
                trace.append('wsl_closed')
                if failure == 'reopened_wsl' and 'vm_activation_check' in trace:
                    raise TransitionBlocked('fixture_wsl_reopened')
                if failure == 'reopened_wsl_before_route' and 'caddy_guard_boundary' in trace:
                    raise TransitionBlocked('fixture_wsl_reopened')

            def assert_reader_settled(self):
                trace.append('readers_settled')

            def restore_writers(self):
                trace.append('restore_wsl')

        class VM:
            def preflight(self, plan, attempt, identity):
                return evidence('vm_preflight', plan, attempt, target_system_id_sha256=hashlib.sha256(
                    identity['target_system_id'].encode()).hexdigest(), restore_ready=True,
                    target_writes_closed=True, target_never_opened=True)

            def restore(self, plan, attempt, source, manifest, target):
                if source != root / attempt / 'frozen' or target != calls['vm_preflight']:
                    raise TransitionBlocked('fixture_wrong_target')
                if failure in ('clean_restore', 'unknown_restore'):
                    trace.append('vm_restore_failure')
                    raise TransitionBlocked('fixture_vm_restore_failed')
                return evidence('vm_restore', plan, attempt, manifest_sha256=manifest, target_sha256=target,
                                target_writes_closed=True, services=module.ALL_SERVICES)

            def verify_closed(self, plan, attempt, manifest, restore):
                return evidence('vm_closed', plan, attempt, manifest_sha256=manifest, restore_sha256=restore,
                                readiness=True, target_writes_closed=True, services=module.ALL_SERVICES)

            def assert_activation_closed(self, plan, attempt, manifest, restore, verified):
                return evidence('vm_activation_check', plan, attempt, manifest_sha256=manifest, restore_sha256=restore,
                                verified_sha256=verified, target_writes_closed=True, target_never_opened=True, restore_ready=True)

            def activate(self, plan, attempt, manifest, verified, settled, *, activation_check_sha256):
                if activation_check_sha256 != calls['vm_activation_check']:
                    raise TransitionBlocked('fixture_activation_unbound')
                return evidence('vm_activate', plan, attempt, manifest_sha256=manifest, verified_sha256=verified,
                                settlement_sha256=settled, activation_check_sha256=activation_check_sha256,
                                vm_write_authority=True)

            def assert_live(self, plan, attempt, authority, **references):
                if references != dict(windows_generation=11, windows_generation_sha256='7' * 64,
                                      live_lease_sha256='8' * 64, live_authority_sha256=calls['vm_live_authority']):
                    raise TransitionBlocked('fixture_wrong_live_authority')
                if failure == 'vm_lease':
                    references['live_lease_sha256'] = '9' * 64
                return evidence('vm_live', plan, attempt, authority_sha256=authority, ready=True,
                                services=module.ALL_SERVICES, **references)

            def observe_rollback(self, plan, attempt, baseline, manifest, restore_attempted):
                status = 'UNKNOWN' if failure == 'unknown_restore' else 'CLEAN_FAILED'
                return evidence('vm_rollback', plan, attempt, status=status, baseline_sha256=baseline,
                                manifest_sha256=manifest, restore_attempted=restore_attempted, target_never_opened=True,
                                target_writes_closed=True, prior_mutations_settled=True)

        facts = {
            'preflight': dict(legacy_live=True, protected_healthy=True, direct_recovery_ready=True,
                              authority_storage_trusted=True, deployment_admission_ready=failure != 'preflight'),
            'direct_recovery_excluded': dict(direct_recovery_excluded=True, recovery_commands_settled=True),
            'authority_storage_trusted': dict(authority_storage_trusted=True),
            'rollback': dict(readers_settled=True, prior_mutations_settled=True, deployment_admission_reconciled=True,
                             legacy_route_unchanged=True),
            'legacy_healthy': dict(legacy_healthy=True, protected_healthy=True),
            'vm_live_authority': dict(active_node='A', windows_generation=11, windows_generation_sha256='7' * 64,
                                      live_lease_sha256='8' * 64),
            'verify_live': dict(protected_healthy=True, external_healthy=True, services=module.ALL_SERVICES),
            'durable_legacy_exclusion': dict(legacy_writers_closed=True, exclusion_durable=True),
        }
        host = SimpleNamespace(guard=hold, observe_external_closed=Mock(),
                               observe=lambda plan, attempt, boundary, prior: evidence(
                                   boundary, plan, attempt, boundary=boundary, prior_sha256=prior, **facts[boundary]),
                               assert_direct_recovery_release_safe=lambda plan, attempt, safety: evidence(
                                   'release_exclusion', plan, attempt, safety_sha256=safety, release_safe=True))

        def windows_request(action, proof):
            trace.append('windows_' + action)
            if failure == 'caddy_guard' and action == 'verify' and 'caddy_guard_boundary' in trace:
                raise TransitionBlocked('fixture_guard_lost')
            return hashlib.sha256(f'{action}:{proof}:{len(trace)}'.encode()).hexdigest()

        def apply_caddy(candidate, assert_guard):
            self.assertEqual(json.loads(journal.read_bytes())['phase'], 'switch_caddy_intent')
            trace.append('caddy_guard_boundary')
            assert_guard()
            trace.append('apply_caddy')
            return {'container_sha256': '5' * 64, 'live_sha256': '6' * 64, 'candidate_sha256': candidate.candidate_sha256}

        caddy = SimpleNamespace(inspect=lambda: {'id': 'fixture-caddy'},
                                prepare=lambda *args, **kwargs: SimpleNamespace(candidate_sha256='4' * 64),
                                assert_original=lambda baseline: trace.append('caddy_original'), apply=apply_caddy)
        windows = SimpleNamespace(guard=hold, request=windows_request, healthy=True)
        admission = SimpleNamespace(close=lambda: trace.append('admission_close'), assert_closed=lambda: None,
                                    restore=lambda *, owned_disable: trace.append('admission_restore'))
        bootstrap = SimpleNamespace(guard=hold, assert_visible=lambda: None, assert_released=lambda: None)
        with patch.object(module, 'NativeFrozenCapture', Capture):
            applications = module.NativeApplications(
                capture_root=root, identity={'plan_sha256': plan, 'target_system_id': '123456'},
                converter_source_pins={}, external_writers={}, caddy=caddy, active_ip='192.0.2.7',
                expected_mtls_sha256={}, vm=VM(), host=host,
            )
            return module.compose_native_cutover(journal, windows, admission, bootstrap, applications).run(plan)

    def test_native_coordinators_chain_manifest_authority_and_caddy_guard(self):
        state = self.run_attempt()
        self.assertEqual(state['phase'], 'complete')
        for stage, proof in (('fence_writers', 'fence'), ('capture', 'capture'), ('restore', 'vm_restore'),
                             ('verify_closed', 'vm_closed'), ('activate_vm', 'vm_activate')):
            self.assertEqual(state['receipts'][stage], self.calls[proof])
        self.assertEqual(self.trace[self.trace.index('vm_activate') - 1], 'wsl_closed')
        self.assertLess(self.trace.index('vm_activate'), self.trace.index('apply_caddy'))
        self.assertLess(self.trace.index('vm_live_authority'), self.trace.index('vm_live'))
        self.assertLess(self.trace.index('vm_live'), self.trace.index('apply_caddy'))
        self.assertLess(self.trace.index('verify_live'), self.trace.index('windows_finish'))

    def test_preflight_and_caddy_guard_block_operational_mutations_at_their_boundaries(self):
        with self.assertRaisesRegex(TransitionBlocked, 'legacy_cutover_preflight_failed'):
            self.run_attempt('preflight')
        self.assertNotIn('admission_close', self.trace)
        with self.assertRaisesRegex(TransitionBlocked, 'legacy_cutover_forward_recovery_required'):
            self.run_attempt('caddy_guard')
        self.assertNotIn('apply_caddy', self.trace)
        self.assertNotIn('restore_wsl', self.trace)

    def test_fresh_wsl_closure_and_bound_windows_lease_gate_activation_and_route(self):
        with self.assertRaisesRegex(TransitionBlocked, 'legacy_cutover_forward_recovery_required'):
            self.run_attempt('reopened_wsl')
        self.assertNotIn('vm_activate', self.trace)
        self.assertNotIn('apply_caddy', self.trace)
        with self.assertRaisesRegex(TransitionBlocked, 'legacy_cutover_forward_recovery_required'):
            self.run_attempt('reopened_wsl_before_route')
        self.assertIn('vm_activate', self.trace)
        self.assertNotIn('vm_live_authority', self.trace)
        self.assertNotIn('apply_caddy', self.trace)
        self.assertNotIn('restore_wsl', self.trace)
        with self.assertRaisesRegex(TransitionBlocked, 'legacy_cutover_forward_recovery_required'):
            self.run_attempt('vm_lease')
        self.assertIn('vm_activate', self.trace)
        self.assertIn('vm_live_authority', self.trace)
        self.assertNotIn('apply_caddy', self.trace)
        self.assertNotIn('restore_wsl', self.trace)

    def test_wsl_rollback_requires_fresh_settlement_for_known_restore_failure(self):
        with self.assertRaisesRegex(TransitionBlocked, 'legacy_cutover_rolled_back'):
            self.run_attempt('clean_restore')
        self.assertEqual(self.trace[:self.trace.index('restore_wsl')].count('vm_rollback'), 2)
        self.assertLess(self.trace.index('restore_wsl'), self.trace.index('admission_restore'))
        with self.assertRaisesRegex(TransitionBlocked, 'legacy_cutover_rollback_reconciliation_required'):
            self.run_attempt('unknown_restore')
        self.assertNotIn('restore_wsl', self.trace)
        self.assertNotIn('admission_restore', self.trace)


if __name__ == "__main__":
    unittest.main()
