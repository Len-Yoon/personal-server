import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from infra.k8s.ha_control import enforced_windows, windows_lease_pipe


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
