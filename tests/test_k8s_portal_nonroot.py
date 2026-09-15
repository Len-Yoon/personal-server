import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CUTOVER = ROOT / "infra/k8s/tools/portal-cutover.sh"
SHADOW = ROOT / "infra/k8s/tools/portal-secret-shadow-smoke.sh"


class PortalNonrootTest(unittest.TestCase):
    def test_local_restore_probe_preserves_sqlite_and_leaves_no_probe_file(self):
        """Exercise the emitted container payload against real files, without Docker access."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            volume = root / "volume"
            volume.mkdir()
            with sqlite3.connect(volume / "homeops.sqlite3") as connection:
                connection.execute("CREATE TABLE notes (value TEXT)")
                connection.execute("INSERT INTO notes VALUES ('preserved')")
            fake_docker = root / "docker"
            fake_docker.write_text(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                "from unittest.mock import patch\n"
                "args = sys.argv[1:]\n"
                "code = args[args.index('-c') + 1]\n"
                "sys.argv = ['probe', os.environ['TEST_VOLUME'], 'homeops.sqlite3']\n"
                "with patch('os.getuid', return_value=10001), patch('os.getgid', return_value=10001):\n"
                "    exec(compile(code, '<container-probe>', 'exec'))\n"
            )
            fake_docker.chmod(0o755)
            library = root / "cutover-lib.sh"
            library.write_text(CUTOVER.read_text().rsplit('\nmain "$@"', 1)[0])
            result = subprocess.run(
                ["bash", "-c", '. "$1"; assert_local_restore_runtime_permissions "$2" homeops.sqlite3',
                 "restore-probe", str(library), str(volume)],
                env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"], "TEST_VOLUME": str(volume)},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([path.name for path in volume.iterdir()], ["homeops.sqlite3"])
            with sqlite3.connect(volume / "homeops.sqlite3") as connection:
                self.assertEqual(connection.execute("SELECT * FROM notes").fetchall(), [("preserved",)])
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone(), ("ok",))

    def test_failed_destination_probe_blocks_copy_before_any_data_stream(self):
        """A non-writable destination must stop preparation before either tar stream."""
        text = CUTOVER.read_text()
        start = text.index('  if ! run_timeout 120 sudo k3s kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/$COPY_POD"')
        end = text.index("  destination_digest=", start)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "cutover-lib.sh"
            library.write_text(text.rsplit('\nmain "$@"', 1)[0])
            result = subprocess.run(
                ["bash", "-c", '. "$1"\n'
                 'run_timeout() { printf "external:%s\\n" "$*" >&2; return 0; }\n'
                 'assert_pvc_runtime_permissions() { printf "probe-denied\\n" >&2; return 1; }\n'
                 'abort_cutover() { printf "aborted\\n" >&2; }\n'
                 'tar() { printf "data-stream-started\\n" >&2; }\n'
                 'test_copy() {\n' + text[start:end] + '\n}\ntest_copy', "probe-order", str(library)],
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("probe-denied", result.stderr)
            self.assertIn("aborted", result.stderr)
            self.assertNotIn("data-stream-started", result.stderr)

    def test_rendered_portal_pods_enforce_nonroot_without_pvc_ownership_mutation(self):
        """A root image override or fsGroup on an existing PVC must fail this contract."""
        identities = []
        for script in (CUTOVER, SHADOW):
            for manifest in re.findall(r"<<YAML\n(.*?)\nYAML", script.read_text(), re.S):
                rendered = subprocess.run(
                    ["bash", "-c", "cat <<YAML\n" + manifest + "\nYAML"],
                    capture_output=True, text=True, check=True,
                ).stdout
                for doc in yaml.safe_load_all(rendered):
                    if doc["kind"] == "Pod":
                        spec = doc["spec"]
                    elif doc["kind"] == "Deployment":
                        spec = doc["spec"]["template"]["spec"]
                    else:
                        continue
                    context = spec.get("securityContext", {})
                    self.assertEqual(context.get("runAsUser"), 10001, script.name)
                    self.assertEqual(context.get("runAsGroup"), 10001, script.name)
                    self.assertIs(context.get("runAsNonRoot"), True, script.name)
                    uses_pvc = any("persistentVolumeClaim" in v for v in spec.get("volumes", []))
                    if uses_pvc:
                        self.assertNotIn("fsGroup", context)
                    else:
                        self.assertEqual(context.get("fsGroup"), 10001)
                    identities.append(doc["kind"])
        self.assertCountEqual(identities, ["Pod", "Pod", "Deployment", "Deployment"])

    def run_permission_probe(self, uid="10001", gid="10001", collision=False, sqlite=False, nested_mode=None, walk_failure=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            volume = root / "volume"
            volume.mkdir()
            sentinel = root / "preserve.txt"
            sentinel.write_text("original-data")
            (volume / "existing.txt").write_text("user-file")
            protected = volume / "nested"
            if nested_mode is not None:
                protected.mkdir()
                protected.chmod(nested_mode)
            if sqlite:
                with sqlite3.connect(volume / "homeops.sqlite3") as connection:
                    connection.execute("CREATE TABLE notes (value TEXT)")
                    connection.execute("INSERT INTO notes VALUES ('original-row')")
            fake_sudo = root / "sudo"
            fake_sudo.write_text(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                "args = sys.argv[1:]\n"
                "if 'exec' not in args: sys.exit(99)\n"
                "command = args[args.index('--') + 1:]\n"
                "if command == ['id', '-u']: print(os.environ['TEST_UID']); sys.exit(0)\n"
                "if command == ['id', '-g']: print(os.environ['TEST_GID']); sys.exit(0)\n"
                "if command[:2] == ['sh', '-c'] and os.environ['TEST_COLLISION'] == '1':\n"
                "    os.symlink(os.environ['TEST_SENTINEL'], os.environ['TEST_VOLUME'] + '/.portal-cutover-permission-probe.' + str(os.getpid()))\n"
                "os.execvp(command[0], command)\n"
            )
            fake_sudo.chmod(0o755)
            if walk_failure:
                (root / "python").write_text(
                    f"#!{sys.executable}\n"
                    "import sys\nfrom unittest.mock import patch\n"
                    "code = sys.argv[2]\nsys.argv = ['-c'] + sys.argv[3:]\n"
                    "with patch('os.scandir', side_effect=OSError('traversal unavailable')):\n"
                    "    exec(compile(code, '<pvc-probe>', 'exec'))\n"
                )
                (root / "python").chmod(0o755)
            else:
                (root / "python").symlink_to(sys.executable)
            library = root / "cutover-lib.sh"
            library.write_text(CUTOVER.read_text().rsplit('\nmain "$@"', 1)[0])
            result = subprocess.run(
                ["bash", "-c", '. "$1"; assert_pvc_runtime_permissions test-pod "$2" "$3"',
                 "probe-test", str(library), str(volume), "homeops.sqlite3" if sqlite else ""],
                env={**os.environ, "PATH": str(root) + os.pathsep + os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"],
                     "TEST_UID": uid, "TEST_GID": gid, "TEST_COLLISION": str(int(collision)),
                     "TEST_SENTINEL": str(sentinel), "TEST_VOLUME": str(volume)},
                capture_output=True, text=True,
            )
            if nested_mode is not None:
                protected.chmod(0o700)
            self.assertEqual(sentinel.read_text(), "original-data")
            self.assertEqual((volume / "existing.txt").read_text(), "user-file")
            leftovers = list(volume.glob(".portal-cutover-permission-probe.*"))
            self.assertEqual(len(leftovers), int(collision and uid == gid == "10001"))
            if sqlite:
                with sqlite3.connect(volume / "homeops.sqlite3") as connection:
                    self.assertEqual(connection.execute("SELECT * FROM notes").fetchall(), [("original-row",)])
                    self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("notes",)])
                    self.assertEqual(connection.execute("PRAGMA quick_check").fetchone(), ("ok",))
            return result

    def test_nonroot_probe_preserves_files_and_rolls_back_sqlite(self):
        result = self.run_permission_probe(sqlite=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_probe_does_not_overwrite_an_existing_name_or_symlink(self):
        result = self.run_permission_probe(collision=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_probe_rejects_root_or_wrong_group(self):
        for uid, gid in (("0", "0"), ("10001", "0"), ("0", "10001")):
            with self.subTest(uid=uid, gid=gid):
                result = self.run_permission_probe(uid=uid, gid=gid)
                self.assertNotEqual(result.returncode, 0)

    def test_pvc_probe_rejects_nested_directory_without_write_or_traverse_access(self):
        for mode in (0o500, 0o600, 0o000):
            with self.subTest(mode=oct(mode)):
                result = self.run_permission_probe(nested_mode=mode)
                self.assertNotEqual(result.returncode, 0)

    def test_pvc_probe_fails_closed_when_directory_traversal_errors(self):
        result = self.run_permission_probe(walk_failure=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
