"""Dedicated boot key enrollment preserves existing SSH access."""

import base64
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'infra/k8s'))
from ha_control import native_boot_ssh_enroll as enroll
from ha_control.native_windows_boot_identity import ED25519_PREFIX


class BootAuthorizationTests(unittest.TestCase):
    key = 'ssh-ed25519 ' + base64.b64encode(ED25519_PREFIX + b'B' * 32).decode()
    prior = b'ssh-ed25519 existing legacy access\n'

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        directory = Path(folder.name).resolve()
        self.keys = directory / 'authorized_keys'
        self.keys.write_bytes(self.prior)
        self.keys.chmod(0o600)
        self.raw_script = b'approved scope entry'
        self.script_pin = enroll.wire._digest(self.raw_script)
        self.script = directory / f'native-boot-ssh-scope-{self.script_pin}.py'
        self.script.write_bytes(self.raw_script)
        self.scope = dict(public_key=self.key, host='192.168.93.4',
                          launcher=str(directory / 'native-guest-worker.py'), launcher_sha256='a' * 64,
                          current_keys_sha256=enroll.wire._digest(self.prior), scope_path=str(self.script),
                          scope_sha256=self.script_pin, guest_boot_sha256=enroll.wire._digest(b'guest boot'),
                          installation_sha256=enroll.wire._digest(b'guest installation'))
        lstat, fstat, read_bytes = Path.lstat, os.fstat, Path.read_bytes

        def visible(info):
            fields = {name: getattr(info, name) for name in ('st_dev', 'st_ino', 'st_mode', 'st_nlink',
                      'st_size', 'st_ctime_ns', 'st_mtime_ns')}
            if stat.S_ISDIR(fields['st_mode']):
                fields['st_mode'] &= ~0o022
            return SimpleNamespace(st_uid=0, **fields)

        mocks = (patch.object(enroll, 'AUTHORIZED', self.keys),
                 patch.object(enroll.scope, 'ROOT', directory),
                 patch.object(enroll.scope, '_read_launcher'),
                 patch.object(enroll, '_read_json', return_value=(b'guest installation', {'node': 'A'})),
                 patch.object(Path, 'lstat', lambda path: visible(lstat(path))),
                 patch.object(enroll.os, 'fstat', lambda fd: visible(fstat(fd))),
                 patch.object(enroll.os, 'geteuid', return_value=0),
                 patch.object(Path, 'read_bytes', lambda path: b'guest boot'
                    if path == Path('/proc/sys/kernel/random/boot_id') else read_bytes(path)))
        for mock in mocks:
            mock.start()
            self.addCleanup(mock.stop)

    def test_append_scoped_key_and_read_back_without_duplicate(self):
        with patch.object(enroll.os, 'fsync', wraps=os.fsync) as flush:
            first = enroll.enroll_vm_a(**self.scope)
            self.assertEqual(enroll.enroll_vm_a(**self.scope), first)
            self.assertEqual(flush.call_count, 2)
        line = self.keys.read_bytes()[len(self.prior):]
        self.assertTrue(line.startswith(b'from="192.168.93.4/32",restrict,command="'))
        self.assertIn(b'/usr/bin/python3 -I -B -S ', line)
        self.assertTrue(line.endswith((self.key + ' n100-ha-windows-boot\n').encode()))
        self.assertEqual(self.keys.read_bytes(), self.prior + line)

    def test_changes_unrestricted_duplicate_and_nonprivate_peer_are_refused(self):
        cases = [
            (self.prior + b'# drift\n', {}),
            (self.prior + self.key.encode() + b'\n', {}),
            (self.prior, {'host': '8.8.8.8'}),
            (self.prior, {'guest_boot_sha256': 'd' * 64}),
            (self.prior, {'installation_sha256': 'c' * 64}),
        ]
        for before, changes in cases:
            with self.subTest(changes=changes):
                self.keys.write_bytes(before)
                with self.assertRaisesRegex(enroll.wire.BridgeRejected, enroll.ERROR):
                    enroll.enroll_vm_a(**(self.scope | changes))
                self.assertEqual(self.keys.read_bytes(), before)

    def test_rejects_if_the_complete_new_file_would_exceed_the_readback_cap(self):
        before = b'#' * 131071 + b'\n'
        self.keys.write_bytes(before)
        with self.assertRaisesRegex(enroll.wire.BridgeRejected, enroll.ERROR):
            enroll.enroll_vm_a(**(self.scope | {'current_keys_sha256': enroll.wire._digest(before)}))
        self.assertEqual(self.keys.read_bytes(), before)
