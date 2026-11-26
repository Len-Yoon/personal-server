"""Host-local, LocalSystem-only PowerShell for the Windows recovery runner."""

from contextlib import contextmanager

from . import native_stdio_bridge as wire
from .native_pipe_process import exchange_direct, start_direct
from .native_windows_ssh import _json_response, _powershell_input
from .windows_protected_io import SYSTEM_POWERSHELL, WindowsProtectedIO


class LocalSystemPowerShell:
    def __init__(self, *, executable_sha256):
        self.custody = WindowsProtectedIO(admin_only=True)
        self._require_system()
        wire._need(wire._hash(executable_sha256), 'local_recovery_powershell_pin_required')
        self.system = self.custody.system_directory_path()
        self.executable = self.system.joinpath(*SYSTEM_POWERSHELL)
        self.executable_sha256 = executable_sha256

    def _require_system(self):
        wire._need(self.custody.owner_sid == 'S-1-5-18', 'local_recovery_system_required')

    @contextmanager
    def live_authority_session(self, deadline):
        """Supply the same per-call proof transport without an SSH session."""
        self._require_system()
        wire._remaining(deadline)
        active = True

        def powershell(source, boundary_deadline):
            wire._need(active, 'local_recovery_authority_session_closed')
            return self.powershell(source, boundary_deadline)

        try:
            yield powershell
        finally:
            active = False

    def powershell(self, source, deadline):
        self._require_system()
        flags, payload = _powershell_input(source)
        wire._remaining(deadline)
        raw, stamp = self.custody.system_executable_stamp(self.executable)
        wire._need(wire._digest(raw) == self.executable_sha256, 'local_recovery_powershell_image_changed')
        system_root = str(self.system.parent)

        def start(command):
            return start_direct(command, cwd=str(self.executable.parent),
                                env={'SystemRoot': system_root, 'WINDIR': system_root,
                                     'PATH': str(self.system)})

        raw = exchange_direct((str(self.executable), *flags), payload, deadline,
                              maximum=1048576, start=start)
        wire._need(self.custody.same_file(self.executable, stamp, private=False),
                   'local_recovery_powershell_image_changed')
        return _json_response(raw)
