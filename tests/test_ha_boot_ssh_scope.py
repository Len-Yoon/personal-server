"""The Windows boot key can invoke only typed VM A recovery requests."""

import hashlib
import io
from pathlib import Path
import shlex
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ha_control import native_boot_ssh_scope as scope, native_guest_ssh as ssh
from ha_control import native_guest_worker as worker, native_stdio_bridge as wire


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
