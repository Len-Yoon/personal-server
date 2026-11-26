"""Prepare a dedicated host-local SSH identity for the Windows boot account."""

import base64
import os
from pathlib import Path

from . import native_stdio_bridge as wire
from .native_guest_ssh import _fingerprint
from .native_pipe_process import exchange_direct, start_direct
from .native_windows_guest_link import WindowsGuestLink
from .windows_protected_io import WindowsProtectedIO


KEY_NAME = 'n100-ha-boot-ed25519'
ERROR = 'local_recovery_boot_identity_unconfirmed'
ED25519_PREFIX = b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20'


def _public_key(raw):
    fields = raw.strip().split()
    wire._need(len(fields) in (2, 3) and fields[0] == b'ssh-ed25519', ERROR)
    blob = base64.b64decode(fields[1], validate=True)
    wire._need(len(blob) == len(ED25519_PREFIX) + 32 and blob.startswith(ED25519_PREFIX)
               and base64.b64encode(blob) == fields[1], ERROR)
    return b' '.join(fields[:2]).decode('ascii'), _fingerprint(blob)


def prepare_system_boot_key(directory, *, keygen_sha256, deadline):
    """Use or create the one dedicated identity. Return only public material.

    The directory is pre-provisioned privately on the Windows host. An
    incomplete key pair is preserved for investigation.
    """
    custody = WindowsProtectedIO(admin_only=True, private_child_creation=True)
    wire._need(custody.owner_sid == 'S-1-5-18' and wire._hash(keygen_sha256), ERROR)
    system = custody.system_directory_path()
    keygen = system / 'OpenSSH' / 'ssh-keygen.exe'
    raw, executable = custody.system_executable_stamp(keygen)
    wire._need(wire._digest(raw) == keygen_sha256, ERROR)
    directory = Path(directory)
    custody.assert_directory(directory, private=True)
    identity = directory / KEY_NAME
    public = identity.with_suffix('.pub')
    intent = directory / 'boot-identity-issued.json'
    expected_intent = wire._bytes({'schema_version': 1, 'keygen_sha256': keygen_sha256, 'key_name': KEY_NAME})
    private_path = WindowsGuestLink._path(identity)

    def start(command):
        system_root = str(system.parent)
        return start_direct(command, cwd=str(keygen.parent),
                            env={'SystemRoot': system_root, 'WINDIR': system_root,
                                 'PATH': str(system)})

    def keygen_call(*arguments):
        wire._need(custody.same_file(keygen, executable, private=False), ERROR)
        raw = exchange_direct((str(keygen), *arguments), b'', deadline, maximum=4096, start=start)
        wire._need(custody.same_file(keygen, executable, private=False), ERROR)
        return raw

    with custody.exclusive(directory / 'boot-identity.lock'):
        has_private, has_public = os.path.lexists(identity), os.path.lexists(public)
        wire._need(has_private == has_public, ERROR)
        if not has_private:
            wire._need(not os.path.lexists(intent), ERROR)
            custody.write_new(intent, expected_intent)
            custody.assert_directory(directory, private=True)
            keygen_call('-q', '-t', 'ed25519', '-N', '', '-C', KEY_NAME, '-f', private_path)
        issued, issued_stamp = custody.read_stamp(intent, 4096, private=True)
        wire._need(issued == expected_intent, ERROR)
        # New OpenSSH files may inherit a safe parent DACL. The validating
        # reader still requires an OS-owned private key and safe parent access.
        keys = WindowsProtectedIO()
        _, private_stamp = keys.read_stamp(identity, 65536, private=True)
        raw_public, public_stamp = keys.read_stamp(public, 4096, private=False)
        supplied = _public_key(raw_public)
        derived = _public_key(keygen_call('-y', '-P', '', '-f', private_path))
        wire._need(supplied == derived and keys.same_file(identity, private_stamp, private=True)
                   and keys.same_file(public, public_stamp, private=False)
                   and custody.same_file(intent, issued_stamp, private=True), ERROR)
        return {'public_key': supplied[0], 'fingerprint': supplied[1]}
