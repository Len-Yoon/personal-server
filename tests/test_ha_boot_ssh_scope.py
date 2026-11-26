"""The Windows boot key can invoke only typed VM A recovery requests."""

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import io
from pathlib import Path
import shlex
import stat
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'infra/k8s'))

from ha_control import initial_app_resume as resume, native_guest_app_operation as app_operation
from ha_control import native_boot_ssh_scope as scope, native_guest_ssh as ssh
from ha_control import native_guest_worker as worker, native_stdio_bridge as wire
from ha_control.native_saved_site_apps import SavedSiteApps


class BootSshScopeTests(unittest.TestCase):
    pin = 'a' * 64

    def command(self, path):
        return shlex.join(('/usr/bin/python3', '-I', '-B', '-S', '-c', ssh.BOOTSTRAP, str(path), self.pin))

    def test_scope_restricts_worker_to_typed_a_requests(self):
        path = scope.ROOT / ('recovery-source-' + self.pin) / scope.NAME
        with patch.object(scope, '_read_launcher', return_value=b'pass\n') as read, \
             patch.object(scope.os, 'geteuid', return_value=0), \
             patch.object(worker, 'main', return_value=0) as run:
            self.assertEqual(scope._run(self.command(path), str(path), self.pin), 0)
        read.assert_called_once_with(path, self.pin)
        run.assert_called_once()
        authorize = run.call_args.kwargs['authorize']
        for node, verb, expected in (
            ('A', wire.GUEST_OBSERVE_INITIAL_RECOVERY_HOLD, True),
            ('A', wire.GUEST_OBSERVE_CAR_ISOLATION, True),
            ('A', wire.GUEST_OBSERVE_INITIAL_POSTBOOT, True),
            ('A', wire.GUEST_START_INITIAL_POSTBOOT, True),
            ('A', wire.GUEST_QUIESCE_INITIAL_POSTBOOT, False),
            ('A', wire.GUEST_START_INITIAL_UNPUBLISHED, False),
            ('B', wire.GUEST_OBSERVE_INITIAL_POSTBOOT, False),
            ('B', wire.GUEST_ACTIVATE_GENERATION, False),
        ):
            with self.subTest(node=node, verb=verb):
                self.assertIs(authorize(SimpleNamespace(node=node, verb=verb)), expected)

    def test_client_script_shell_and_command_drift_are_rejected(self):
        path = scope.ROOT / scope.NAME
        valid = self.command(path)
        script = shlex.join(('/usr/bin/python3', '-I', '-B', '-S', '-c', ssh.STDIN_SCRIPT, '10', self.pin))
        with patch.object(scope, '_read_launcher', return_value=b'pass\n'), \
             patch.object(scope.os, 'geteuid', return_value=0), patch.object(worker, 'main') as run:
            for original in (script, 'id', '', valid + '; id', valid + '\n', valid.replace(self.pin, 'b' * 64)):
                with self.subTest(original=original), self.assertRaises(ValueError):
                    scope._run(original, str(path), self.pin)
            run.assert_not_called()

    def test_canonical_protected_launcher_path(self):
        boot = scope.ROOT / scope.NAME
        self.assertEqual(scope._launcher_path(str(boot)), boot)
        for path in (f'{boot}/', f'{scope.ROOT}/../cutover/{scope.NAME}',
                     f'{scope.ROOT}/incoming/{scope.NAME}', '/tmp/native-guest-worker.py'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                scope._launcher_path(path)

    def test_launcher_requires_exact_file_custody_pin_and_entrypoint(self):
        path = scope.ROOT / scope.NAME
        raw = b'pass\n' + scope.ENTRY + b'\n'
        parent_info = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        file_info = SimpleNamespace(st_mode=stat.S_IFREG | 0o400, st_uid=0, st_nlink=1,
                                    st_size=len(raw), st_ctime_ns=10)

        def read(data, digest, *, parent=parent_info, before=file_info, after=file_info):
            source = Mock(wraps=io.BytesIO(data))
            source.fileno.return_value = 29
            descriptor = Mock()
            descriptor.__enter__ = Mock(return_value=source)
            descriptor.__exit__ = Mock(return_value=False)
            with patch.object(Path, 'lstat', return_value=parent), \
                 patch.object(scope.os, 'open', return_value=29) as op, \
                 patch.object(scope.os, 'fstat', side_effect=(before, after)), \
                 patch.object(scope.os, 'fdopen', return_value=descriptor):
                result = scope._read_launcher(path, digest)
                self.assertTrue(op.call_args.args[1] & scope.os.O_NOFOLLOW)
                return result

        digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual(read(raw, digest), b'pass\n')
        wrong_entry = raw[:-2] + b'#\n'
        late_change = SimpleNamespace(**(vars(file_info) | {'st_ctime_ns': 11}))
        hardlink = SimpleNamespace(**(vars(file_info) | {'st_nlink': 2}))
        symlink_parent = SimpleNamespace(**(vars(parent_info) | {'st_mode': stat.S_IFLNK | 0o755}))
        for data, pin, extra in (
            (raw, self.pin, {}),
            (wrong_entry, hashlib.sha256(wrong_entry).hexdigest(), {}),
            (raw, digest, {'after': late_change}),
            (raw, digest, {'before': hardlink}),
            (raw, digest, {'parent': symlink_parent}),
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                read(data, pin, **extra)

    def test_worker_rejects_before_mutating_or_opening_duplex_channel(self):
        request = SimpleNamespace(node='A', verb=wire.GUEST_START_INITIAL_POSTBOOT)
        with patch.object(worker, 'read_request', return_value=request), patch.object(worker, 'handle') as handle, \
             patch.object(worker, 'Stdio') as channel, patch.object(worker.signal, 'alarm') as alarm:
            self.assertEqual(worker.main(authorize=lambda candidate: candidate is not request), 1)
        handle.assert_not_called()
        channel.assert_not_called()
        self.assertEqual([call.args for call in alarm.call_args_list], [(10,), (0,)])


class BootAppOperationTests(unittest.TestCase):
    def fixture(self):
        from tests.test_ha_initial_app_resume import SuccessorAppWiringTests

        return SuccessorAppWiringTests().fixture()

    def test_typed_request_scope_and_reply_binding(self):
        _, stage, request = self.fixture()
        inputs = {'instance': stage.lease.service_instance_sha256, 'intent': stage.prior_intent,
                  'progress': stage.prior_progress, 'prior_resume': None}
        payload = app_operation.encode_request(app_operation.ATTEST, request, inputs)
        operation = worker.read_request(io.BytesIO(payload), allow_boot_app=True)
        self.assertEqual(operation, app_operation.Operation(app_operation.ATTEST, request, inputs, wire._digest(payload)))
        with self.assertRaises((wire.BridgeRejected, KeyError)):
            worker.read_request(io.BytesIO(payload))
        with self.assertRaises(worker.TransitionBlocked):
            worker.read_request(io.BytesIO(payload + b'{}\n'), allow_boot_app=True)
        for changed in (
            replace(request, node='B'),
            replace(request, verb=wire.GUEST_START_INITIAL_UNPUBLISHED),
            replace(request, args=request.args | {'shell': 'id'}),
        ):
            with self.subTest(changed=changed), self.assertRaises(wire.BridgeRejected):
                app_operation.encode_request(app_operation.ATTEST, changed, inputs)
        with self.assertRaises(wire.BridgeRejected):
            app_operation.encode_request('execute', request, inputs)

        with patch.object(resume, 'attest', return_value={'authorized': True}) as attest:
            reply = app_operation.reply(operation)
        attest.assert_called_once_with(request, service_instance_sha256=inputs['instance'],
                                       prior_intent_sha256=inputs['intent'], prior_progress_sha256=inputs['progress'],
                                       prior_resume=None)
        self.assertEqual(app_operation.decode_reply(reply, app_operation.ATTEST, payload), {'authorized': True})
        with self.assertRaises(wire.BridgeRejected):
            app_operation.decode_reply(reply, app_operation.ATTEST, payload + b'\n')

    def test_typed_worker_applies_same_forced_authorization(self):
        _, stage, request = self.fixture()
        inputs = {'instance': stage.lease.service_instance_sha256, 'intent': stage.prior_intent,
                  'progress': stage.prior_progress, 'prior_resume': None}
        payload = app_operation.encode_request(app_operation.ATTEST, request, inputs)
        result = io.BytesIO()
        with patch.object(worker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(payload))), \
             patch.object(worker.sys, 'stdout', SimpleNamespace(buffer=result)), \
             patch.object(worker.signal, 'alarm'), patch.object(resume, 'attest') as attest:
            self.assertEqual(worker.main(authorize=lambda candidate: False), 1)
            attest.assert_not_called()
        with patch.object(worker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(payload))), \
             patch.object(worker.sys, 'stdout', SimpleNamespace(buffer=result)), \
             patch.object(worker.signal, 'alarm'), patch.object(resume, 'attest') as attest:
            self.assertEqual(worker.main(), 1)
            attest.assert_not_called()
        self.assertEqual(result.getvalue(), b'')

    def test_windows_attestation_readback_and_completion_use_pinned_command_without_script(self):
        app, stage, request = self.fixture()
        dispatcher = stage.dispatcher
        dispatcher.guests = {'A': object()}
        dispatcher.known_hosts = {'A': object()}
        launcher = b'# ' + stage.manifest.encode() + b'\n' + scope.ENTRY + b'\n'
        dispatcher.launcher_sha256 = wire._digest(launcher)
        dispatcher.command = shlex.join(('/usr/bin/python3', '-I', '-B', '-S', '-c', ssh.BOOTSTRAP,
                                         ssh.WORKER, dispatcher.launcher_sha256))

        @contextmanager
        def connect(guest, known_hosts, command, deadline):
            self.assertIs(guest, dispatcher.guests['A'])
            self.assertIs(known_hosts, dispatcher.known_hosts['A'])
            self.assertEqual(command, dispatcher.command)
            yield 'pinned-command'

        observed = []

        def exchange(command, payload, deadline):
            self.assertEqual(command, 'pinned-command')
            operation = worker.read_request(io.BytesIO(payload), allow_boot_app=True)
            observed.append(operation.name)
            return app_operation.reply(operation)

        dispatcher.direct_link = SimpleNamespace(connection=connect, exchange=exchange, scoped_boot_key=True)
        authorization = {'schema_version': 1, 'authorization_sha256': '3' * 64, 'request_nonce': request.nonce,
                         'baseline_sha256': '4' * 64, 'prior_intent_sha256': stage.prior_intent,
                         'prior_progress_sha256': stage.prior_progress,
                         'service_instance_sha256': stage.lease.service_instance_sha256,
                         'container_count': 8, 'containers_started': False, 'database_changed': False}
        expected = 'b' * 64
        completion = {'status': 'STARTED_VERIFIED'}
        guard = Mock()
        deadline = time.monotonic() + 10
        saved_apps = object.__new__(SavedSiteApps)
        saved_apps.previous, saved_apps.terminal, saved_apps.history = request, b'terminal', {}
        saved_apps.pair = SimpleNamespace(config={'binding_sha256': 'd' * 64})
        saved_apps.closed_revision, saved_apps.guard, saved_apps.app = 15, guard, stage
        saved_apps.dispatcher = dispatcher
        with patch.object(app, '_guest_script') as dynamic, patch.object(resume, 'attest', return_value=authorization), \
             patch.object(app_operation, 'read_authorization', return_value=authorization) as read_auth, \
             patch.object(app_operation, 'observe_completion', return_value=completion) as read:
            actual = app.guest_attest(dispatcher, request, launcher, stage.manifest, stage.lease.service_instance_sha256,
                                     stage.prior_intent, stage.prior_progress, deadline, assert_guard=guard)
            self.assertEqual(actual, authorization)
            self.assertEqual(saved_apps._guest('authorization', request, deadline), authorization)
            self.assertEqual(app.guest_completion(dispatcher, request, launcher, stage.manifest, authorization, deadline,
                                                 assert_guard=guard, expected_receipt_sha256=expected), completion)
        read_auth.assert_called_once_with(request, instance=stage.lease.service_instance_sha256,
                                          intent=stage.prior_intent, progress=stage.prior_progress)
        actual_request, = read.call_args.args
        self.assertEqual(actual_request, request)
        self.assertEqual(read.call_args.kwargs['authorization'], {key: authorization[key] for key in app_operation.AUTH_FIELDS})
        self.assertEqual(read.call_args.kwargs['expected'], expected)
        self.assertEqual(observed, [app_operation.ATTEST, app_operation.READ_AUTH, app_operation.COMPLETE])
        self.assertEqual(guard.call_count, 6)
        dynamic.assert_not_called()
        with self.assertRaises(wire.BridgeRejected):
            dispatcher.exchange_direct_script('A', 'assert False', b'', deadline)

    def test_lost_attestation_readback_rejects_changed_record_without_repeating_attestation(self):
        _, stage, request = self.fixture()
        boot = b'boot'
        request = replace(request, binding=replace(request.binding, boot_a_sha256=wire._digest(boot)))
        inputs = {'instance': stage.lease.service_instance_sha256, 'intent': stage.prior_intent,
                  'progress': stage.prior_progress}
        stored = {'service_instance_sha256': inputs['instance'], 'prior_intent_sha256': inputs['intent'],
                  'prior_progress_sha256': inputs['progress'], 'container_contracts': {}}
        raw = wire._bytes(stored)
        closed = ('hold-raw', {}, {}, {})
        with patch.object(worker.os, 'geteuid', return_value=0), \
             patch.object(worker.runtime, 'BOOT_ID', Mock(read_bytes=Mock(return_value=boot))), \
             patch.object(worker, '_guest_mutation_descriptor'), \
             patch.object(worker, '_read_initial_recovery_hold', return_value=closed), \
             patch.object(resume, '_validate_authorization') as validate, \
             patch.object(resume, 'preserved') as preserved, \
             patch.object(resume, 'attest') as attest, \
             patch.object(worker.runtime, '_read', side_effect=(raw, raw, raw, raw + b' ')):
            observed = app_operation.read_authorization(request, **inputs)
            self.assertEqual(observed['authorization_sha256'], wire._digest(raw))
            self.assertIs(observed['containers_started'], False)
            with self.assertRaises(wire.BridgeRejected):
                app_operation.read_authorization(request, **inputs)
        attest.assert_not_called()
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(preserved.call_count, 2)
