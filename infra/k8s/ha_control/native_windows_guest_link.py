"""Direct OpenSSH into a pinned guest from the Windows recovery host."""

import base64
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import re
import sys
import tempfile
from subprocess import list2cmdline

from . import native_stdio_bridge as bridge
from .native_pipe_process import exchange_direct, start_direct


class WindowsGuestLink:
    """Consume existing authorized key files. Never create or import identities."""

    def __init__(self, directory, *, ssh_sha256, keygen_sha256, scoped_boot_key=False):
        bridge._need(sys.platform == 'win32' and bridge._hash(ssh_sha256) and bridge._hash(keygen_sha256)
                     and type(scoped_boot_key) is bool,
                     'guest_windows_ssh_scope_rejected')
        from .windows_protected_io import WindowsProtectedIO

        system = ctypes.windll.kernel32.GetSystemDirectoryW
        buffer = ctypes.create_unicode_buffer(32768)
        count = system(buffer, len(buffer))
        bridge._need(0 < count < len(buffer), 'guest_windows_ssh_path_rejected')
        self.system = Path(buffer.value)
        root = self.system / 'OpenSSH'
        self.ssh, self.keygen = root / 'ssh.exe', root / 'ssh-keygen.exe'
        self.pins = {self.ssh: ssh_sha256, self.keygen: keygen_sha256}
        self.scoped_boot_key = scoped_boot_key
        self.directory = Path(directory)
        self.local = WindowsProtectedIO()
        self.local.assert_directory(self.directory, private=True)

    @staticmethod
    def _path(path):
        value = Path(path).as_posix()
        bridge._need(re.fullmatch(r'[A-Za-z]:/[A-Za-z0-9_./ -]+', value) and '..' not in Path(path).parts,
                     'guest_windows_ssh_path_rejected')
        return value

    def exchange(self, command, payload, deadline):
        return exchange_direct(command, payload, deadline, maximum=bridge.FRAME_BYTES, start=self.start)

    def start(self, command):
        bridge._need(command[0] in {str(self.ssh), str(self.keygen)}, 'guest_windows_ssh_executable_unconfirmed')
        root = str(self.system.parent)
        return start_direct(command, cwd=str(self.system),
                            env={'SystemRoot': root, 'WINDIR': root, 'PATH': str(self.system)})

    @contextmanager
    def connection(self, guest, known_hosts, worker_command, deadline):
        from .native_guest_ssh import ALGORITHMS, SshEndpoint, _fingerprint, _host_record

        bridge._need(type(guest) is SshEndpoint, 'guest_windows_ssh_scope_rejected')
        guest.validate(guest=True)
        self.local.assert_directory(self.directory, private=True)
        input_stamps = {}
        for path, pin in self.pins.items():
            raw, stamp = self.local.system_executable_stamp(path)
            bridge._need(bridge._digest(raw) == pin, 'guest_windows_ssh_executable_unconfirmed')
            input_stamps[path] = (stamp, False)
        identity, known_hosts = Path(guest.identity_file), Path(known_hosts)
        _, stamp = self.local.read_stamp(identity, 65536, private=True)
        input_stamps[identity] = (stamp, True)
        raw, stamp = self.local.read_stamp(known_hosts, 1048576, private=False)
        input_stamps[known_hosts] = (stamp, False)
        kind, encoded = _host_record(raw, guest, guest=True)
        raw = self.exchange((str(self.keygen), '-y', '-P', '', '-f', self._path(identity)), b'', deadline)
        fields = raw.split()
        bridge._need(len(fields) >= 2, 'guest_ssh_identity_fingerprint_mismatch')
        public = base64.b64decode(fields[1], validate=True)
        bridge._need(_fingerprint(public) == guest.identity_fingerprint, 'guest_ssh_identity_fingerprint_mismatch')
        sidecar = identity.with_name(identity.name + '.pub')
        cert = identity.with_name(identity.name + '-cert.pub')
        absent = [cert]
        if os.path.lexists(sidecar):
            raw, stamp = self.local.read_stamp(sidecar, 65536, private=False)
            fields = raw.split()
            bridge._need(len(fields) >= 2 and base64.b64decode(fields[1], validate=True) == public,
                         'guest_ssh_identity_fingerprint_mismatch')
            input_stamps[sidecar] = (stamp, False)
        else:
            absent.append(sidecar)

        with tempfile.TemporaryDirectory(prefix='n100-guest-ssh-', dir=self.directory) as folder:
            folder = Path(folder)
            self.local.assert_directory(folder, private=True)
            trust = folder / 'known_hosts'
            trust_raw = f'{guest.alias} {kind} {encoded}\n'.encode('ascii')
            self.local.write_new(trust, trust_raw)
            actual, stamp = self.local.read_stamp(trust, 4096, private=True)
            bridge._need(actual == trust_raw, 'guest_windows_ssh_trust_unconfirmed')
            input_stamps[trust] = (stamp, True)
            options = {'HostKeyAlias': guest.alias, 'UserKnownHostsFile': '"' + self._path(trust) + '"',
                       'HostKeyAlgorithms': ALGORITHMS[kind], 'GlobalKnownHostsFile': 'none',
                       'ProxyCommand': 'none', 'ProxyJump': 'none', 'IdentityAgent': 'none',
                       'IdentitiesOnly': 'yes', 'CertificateFile': 'none', 'BatchMode': 'yes',
                       'PasswordAuthentication': 'no', 'KbdInteractiveAuthentication': 'no',
                       'HostbasedAuthentication': 'no',
                       'PreferredAuthentications': 'publickey', 'StrictHostKeyChecking': 'yes',
                       'UpdateHostKeys': 'no', 'VerifyHostKeyDNS': 'no', 'ConnectionAttempts': '1',
                       'ConnectTimeout': '5', 'LogLevel': 'ERROR', 'ClearAllForwardings': 'yes',
                       'ForwardAgent': 'no', 'ForwardX11': 'no', 'PermitLocalCommand': 'no'}
            flags = tuple(part for key, value in options.items() for part in ('-o', f'{key}={value}'))
            command = (str(self.ssh), '-F', 'none', '-T', *flags, '-i', self._path(identity),
                       '-p', str(guest.port), '-l', guest.user, guest.host, worker_command)
            bridge._need(len(list2cmdline(command).encode('utf-16-le')) // 2 < 32767,
                         'guest_windows_ssh_command_too_long')

            def current():
                bridge._need(not any(os.path.lexists(path) for path in absent)
                             and all(self.local.same_file(path, pin, private=private)
                                 for path, (pin, private) in input_stamps.items()), 'guest_ssh_local_file_changed')

            current()
            yield command
            current()
