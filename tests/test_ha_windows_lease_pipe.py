import os
from pathlib import Path
from queue import Queue
import tempfile
from threading import Lock
import unittest
from unittest.mock import Mock, patch

from infra.k8s.ha_control import enforced_windows, initial_lease_service, windows_lease_pipe


class WindowsLeasePipePathTests(unittest.TestCase):
    def test_only_exact_system_powershell_allows_hardlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            powershell = root / "powershell.exe"
            alias = root / "other.exe"
            powershell.write_bytes(b"fixture")
            os.link(powershell, alias)

            with patch.object(windows_lease_pipe.sys, "platform", "win32"), patch.object(
                enforced_windows, "_powershell_path", return_value=powershell
            ):
                self.assertEqual(windows_lease_pipe._protected_path(powershell, system_powershell=True).st_nlink, 2)
                with self.assertRaises(ValueError):
                    windows_lease_pipe._protected_path(alias, system_powershell=True)
                with self.assertRaises(ValueError):
                    windows_lease_pipe._protected_path(powershell)

    def test_failed_observation_records_only_the_rejection_stage(self):
        for frame, code in ((b"", "legacy_pipe_response_missing"),
                            (b"failure connect\r\n", "legacy_pipe_client_connect"),
                            (b"failure read\r\n", "legacy_pipe_client_read"),
                            (b"failure unknown\r\n", "legacy_pipe_response_invalid"),
                            (b"untrusted payload\n", "legacy_pipe_response_invalid")):
            with self.subTest(code=code):
                client = object.__new__(windows_lease_pipe.PersistentLeaseProofClient)
                client._lock = Lock()
                client._process = Mock()
                client._process.poll.return_value = None
                client._incoming = Queue()
                client._outgoing = Queue()
                client._key = bytearray(32)
                client.monotonic = lambda: 0
                client.pins = Mock()
                client.pins.challenge.return_value = {}
                client.close = Mock()
                client._incoming.put(frame)
                observer = initial_lease_service.WindowsInitialObserver(client, None, None, None)
                with self.assertRaisesRegex(initial_lease_service.TransitionBlocked, f"^{code}$"):
                    observer.assert_legacy_closed({}, 2)
                self.assertIn(code, initial_lease_service.LEASE_FAILURE_CODES)
                client.close.assert_called_once_with()
